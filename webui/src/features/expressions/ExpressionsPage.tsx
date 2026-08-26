/**
 * 表达方式页：浏览词表并做人工复核。
 *
 * 「在用」与「在学」是两件事，页面必须分开表述，否则读者只能从 useCount
 * 猜——而那个数含历史迁移带入的存量，猜不出来：
 * - 在用：已接入回复生成（候选池加权抽样 → 选择模型挑一条 → 注入提示词），
 *   选中会回写 use_count 与 last_used_at。行内「本机用过」徽标只在
 *   last_used_at 非空时出现，它是本部署真实用过的唯一证据。
 * - 在学：回合收尾处的后台任务从真实对话里学新说法（source 为「本机学习」），
 *   并按确定性规则淘汰从未被本机选中且过了保留期的条目。
 *
 * 人工复核不是使用的前置条件（未复核照常进候选池），它的职责是剔除与保护：
 * 确认（checked=1）的永不自动淘汰，驳回（checked=-1）的退出候选池并整行
 * 置灰删除线标示「不再生效」。复核状态可筛选，用于从几千条里找出待处理的。
 *
 * 列表按使用次数排序，一行一条。
 */
import { useState } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import {
  Button,
  Card,
  CardBody,
  Chip,
  Empty,
  ErrorText,
  Field,
  Loading,
  Pager,
  Select,
  SegmentedTabs,
  cn,
} from '@/components/ui'
import {
  setExpressionChecked,
  useExpressions,
  type ExpressionChecked,
  type ExpressionEntry,
} from '@/hooks/use-expressions'
import { useAuth } from '@/hooks/use-auth'
import { useStreams } from '@/hooks/use-observability'
import { dateTime, streamLabel } from '@/lib/format'
import { UnauthorizedError } from '@/lib/api'

/** 页大小；一屏多一点为宜，太长要一直滚。后端路由 le=200，取值留足余量。 */
const PAGE_SIZE = 20

/** 复核状态的可见标记；未复核不挂徽标，它是默认态。 */
const CHECKED_LABEL: Record<string, string> = {
  '1': '已确认',
  '-1': '已驳回 · 不再生效',
}

/**
 * 渲染单条表达方式行，含复核动作。
 *
 * @param props.entry 表达数据。
 * @param props.streamLabelOf 按 streamId 取会话标签的函数。
 * @param props.pending 该行是否有复核写入在飞。
 * @param props.onReview 点击确认/驳回时的回调。
 * @returns 一行表达元素。
 */
function ExpressionRow({
  entry,
  streamLabelOf,
  pending,
  onReview,
}: {
  entry: ExpressionEntry
  streamLabelOf: (id: number) => string
  pending: boolean
  onReview: (id: number, checked: ExpressionChecked) => void
}) {
  const rejected = entry.checked === -1
  return (
    <li
      className={cn(
        'flex flex-col gap-1.5 border-b border-border/50 px-4 py-3 last:border-b-0',
        rejected && 'opacity-55',
      )}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span
          className={cn('text-[15px] font-semibold', rejected && 'line-through')}
        >
          「{entry.style}」
        </span>
        <Chip label="用过" value={`${entry.useCount} 次`} />
        {entry.lastUsedAt !== null ? (
          <Chip label="本机用过" value={dateTime(entry.lastUsedAt)} />
        ) : null}
        {entry.streamId === null ? (
          <Chip label="范围" value="全局通用" />
        ) : (
          <Chip label="范围" value={streamLabelOf(entry.streamId)} />
        )}
        {entry.checked !== 0 ? <Chip label="复核" value={CHECKED_LABEL[String(entry.checked)]} /> : null}
        <span className="ml-auto font-mono text-[11px] text-muted-foreground">{entry.source}</span>
        <Button
          variant="secondary"
          size="sm"
          disabled={pending || entry.checked === 1}
          onClick={() => onReview(entry.id, 1)}
        >
          确认
        </Button>
        <Button
          variant="danger-outline"
          size="sm"
          disabled={pending || entry.checked === -1}
          onClick={() => onReview(entry.id, -1)}
        >
          驳回
        </Button>
      </div>
      <p className="text-sm leading-relaxed text-muted-foreground">{entry.situation}</p>
    </li>
  )
}

/**
 * 渲染表达方式页。
 *
 * @returns 页面容器元素；筛选变化自动回到第一页。
 */
export function ExpressionsPage() {
  const { handleUnauthorized } = useAuth()
  const { streams, error: streamsError } = useStreams()
  /** all=全部会话；其余为具体会话 ID。 */
  const [scope, setScope] = useState('all')
  /** all=不限复核状态；其余为 0/1/-1。 */
  const [checkedFilter, setCheckedFilter] = useState('all')
  const [order, setOrder] = useState<'use_desc' | 'use_asc'>('use_desc')
  const [page, setPage] = useState(0)
  /** 复核写入后递增，触发列表重新拉取。 */
  const [refreshKey, setRefreshKey] = useState(0)
  /** 正在写入复核状态的行 id；在飞期间禁用该行两个动作。 */
  const [pendingId, setPendingId] = useState<number | null>(null)
  const [reviewError, setReviewError] = useState('')

  const streamId = scope !== 'all' ? Number(scope) : null
  const checked = checkedFilter !== 'all' ? (Number(checkedFilter) as ExpressionChecked) : null
  const { entries, total, loading, error } = useExpressions({
    streamId,
    checked,
    order,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    refreshKey,
  })

  const streamLabelOf = (id: number) => {
    const stream = streams.find((item) => item.id === id)
    return stream ? streamLabel(stream) : `会话 #${id}`
  }

  const changeFilter = (apply: () => void) => {
    apply()
    setPage(0)
  }

  const review = async (id: number, next: ExpressionChecked) => {
    setPendingId(id)
    setReviewError('')
    try {
      await setExpressionChecked(id, next)
      setRefreshKey((key) => key + 1)
    } catch (err: unknown) {
      if (err instanceof UnauthorizedError) handleUnauthorized(err)
      else setReviewError(`复核写入失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setPendingId(null)
    }
  }

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI · CONSOLE"
        title="表达方式"
        subtitle="她说话时可选的说法与情境，按使用次数排序。"
      />
      <div className="flex flex-wrap items-end gap-3">
        <SegmentedTabs
          tabs={[
            { value: 'use_desc', label: '用得最多' },
            { value: 'use_asc', label: '用得最少' },
          ]}
          value={order}
          onChange={(next) => changeFilter(() => setOrder(next))}
        />
        <SegmentedTabs
          tabs={[
            { value: 'all', label: '全部' },
            { value: '0', label: '未复核' },
            { value: '1', label: '已确认' },
            { value: '-1', label: '已驳回' },
          ]}
          value={checkedFilter}
          onChange={(next) => changeFilter(() => setCheckedFilter(next))}
        />
        <Field label="会话" htmlFor="expressions-scope" className="w-64">
          <Select
            id="expressions-scope"
            value={scope}
            onChange={(event) => changeFilter(() => setScope(event.target.value))}
          >
            <option value="all">全部会话</option>
            {streams.map((stream) => (
              <option key={stream.id} value={stream.id}>
                {streamLabel(stream)}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      {streamsError ? <ErrorText>{streamsError}</ErrorText> : null}
      {error ? <ErrorText>{error}</ErrorText> : null}
      {reviewError ? <ErrorText>{reviewError}</ErrorText> : null}
      {loading ? <Loading>正在读取表达方式…</Loading> : null}
      {!loading && !error && entries.length === 0 ? <Empty>没有符合条件的表达方式。</Empty> : null}
      {entries.length > 0 ? (
        <Card>
          <CardBody className="p-0">
            <ol>
              {entries.map((entry) => (
                <ExpressionRow
                  key={entry.id}
                  entry={entry}
                  streamLabelOf={streamLabelOf}
                  pending={pendingId === entry.id}
                  onReview={review}
                />
              ))}
            </ol>
          </CardBody>
        </Card>
      ) : null}

      <Pager page={page} pageCount={pageCount} total={total} onChange={setPage} disabled={loading} />
    </div>
  )
}
