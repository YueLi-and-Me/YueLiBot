/**
 * 表达方式页：浏览词表并做人工复核。
 *
 * 「在用」与「在学」是两件事，页面必须分开表述，否则读者只能从 useCount
 * 猜——而那个数含历史迁移带入的存量，猜不出来：
 * - 在用：已接入回复生成（候选池加权抽样 → 选择模型挑一条 → 注入提示词），
 *   选中会回写 use_count 与 last_used_at。行内「本机用过」徽标只在
 *   last_used_at 非空时出现，它是本部署真实用过的唯一证据。
 * - 在学：回合收尾处的后台任务从真实对话里学新说法（source 为「本机学习」），
 *   并按确定性规则淘汰其中从未被选中且过了保留期的条目。
 *   **自动淘汰只碰本机学来的行**，迁移存量一条都不自动删——存量里有整个会话
 *   的行从未被本机选中过，自动清理会让该会话候选池归零、表达选择停摆。
 *
 * 三种行内动作各管一件事，不要混用：
 * - 确认（checked=1）：永不自动淘汰，用于锁住特别贴的说法；
 * - 驳回（checked=-1）：退出候选池但保留行，整行置灰加删除线标示「不再生效」。
 *   这一行同时是「判过了」的记号，学习器再学到同样的说法会被唯一约束挡住；
 * - 删除：不可逆地移除，同样的说法日后可以被重新学到。清理迁移存量走这条。
 *   行首勾选框喂的是批量删除，选择集跨页保留、筛选变化时清空；批量删除后端
 *   会回报候选数跌破起用下限的会话，那些会话的表达注入会直接停摆，页面必须
 *   如实报出来而不是替人拦下删除。
 *
 * 人工复核不是使用的前置条件（未复核照常进候选池），它的职责是剔除与保护。
 * 复核状态可筛选，用于从几千条里找出待处理的。
 *
 * 列表按使用次数排序，一行一条。
 */
import { useState } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import {
  Button,
  ConfirmDialog,
  Card,
  CardBody,
  Checkbox,
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
  deleteExpression,
  deleteExpressions,
  setExpressionChecked,
  useExpressions,
  type ExpressionChecked,
  type ExpressionEntry,
  type LowPool,
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
 * @param props.pending 该行是否有写入在飞（复核或删除）。
 * @param props.selected 该行是否已被勾选进批量删除的选择集。
 * @param props.onReview 点击确认/驳回时的回调。
 * @param props.onDelete 点击删除时的回调；由调用方弹确认框，本组件只发起。
 * @param props.onToggleSelect 勾选框切换时的回调，参数为行 ID。
 * @returns 一行表达元素。
 */
function ExpressionRow({
  entry,
  streamLabelOf,
  pending,
  selected,
  onReview,
  onDelete,
  onToggleSelect,
}: {
  entry: ExpressionEntry
  streamLabelOf: (id: number) => string
  pending: boolean
  selected: boolean
  onReview: (id: number, checked: ExpressionChecked) => void
  onDelete: (entry: ExpressionEntry) => void
  onToggleSelect: (id: number) => void
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
        {/* 勾选框在行首，只喂批量删除；行内确认/驳回/删除与选择集互不影响。 */}
        <Checkbox
          checked={selected}
          onChange={() => onToggleSelect(entry.id)}
          disabled={pending}
        />
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
        {/* 行内删除取 ghost：与表情包页同惯例，危险性由确认弹窗承担，
            按钮本身不抢视线。 */}
        <Button variant="ghost" size="sm" disabled={pending} onClick={() => onDelete(entry)}>
          删除
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
  /** 正在写入的行 id（复核或删除）；在飞期间禁用该行全部动作。 */
  const [pendingId, setPendingId] = useState<number | null>(null)
  const [reviewError, setReviewError] = useState('')
  /** 待确认删除的行；null 表示确认弹窗关闭。删除不可逆，必须过一道确认。 */
  const [pendingDelete, setPendingDelete] = useState<ExpressionEntry | null>(null)
  /** 已勾选待批量删除的行 ID。跨页保留（可翻几页攒一批再删），筛选变化时清空
      ——换了筛选条件后选择集里剩什么已经看不见了，留着等于埋雷。 */
  const [selected, setSelected] = useState<Set<number>>(new Set())
  /** 批量删除确认弹窗开关。 */
  const [batchConfirm, setBatchConfirm] = useState(false)
  /** 批量删除在飞；期间禁用整个工具栏。 */
  const [batchPending, setBatchPending] = useState(false)
  /** 上一次批量删除后候选数跌破下限的会话，由后端回报。非空说明那些会话的
      表达注入已经停摆，必须持续显示到下一次删除为止。 */
  const [lowPools, setLowPools] = useState<LowPool[]>([])

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
    setSelected(new Set())
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

  const removeEntry = async (entry: ExpressionEntry) => {
    setPendingDelete(null)
    setPendingId(entry.id)
    setReviewError('')
    try {
      await deleteExpression(entry.id)
      setRefreshKey((key) => key + 1)
    } catch (err: unknown) {
      if (err instanceof UnauthorizedError) handleUnauthorized(err)
      else setReviewError(`删除失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setPendingId(null)
    }
  }

  const toggleSelect = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const pageIds = entries.map((entry) => entry.id)
  const pageAllSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id))

  const toggleSelectPage = () => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (pageAllSelected) pageIds.forEach((id) => next.delete(id))
      else pageIds.forEach((id) => next.add(id))
      return next
    })
  }

  const removeSelected = async () => {
    setBatchConfirm(false)
    setBatchPending(true)
    setReviewError('')
    try {
      const result = await deleteExpressions([...selected])
      setSelected(new Set())
      setLowPools(result.lowPools)
      // 删完当前页可能整页落空，把页码夹回新的末页，避免停在空白分页上。
      const nextPageCount = Math.max(1, Math.ceil((total - result.deleted) / PAGE_SIZE))
      setPage((current) => Math.min(current, nextPageCount - 1))
      setRefreshKey((key) => key + 1)
    } catch (err: unknown) {
      if (err instanceof UnauthorizedError) handleUnauthorized(err)
      else setReviewError(`批量删除失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBatchPending(false)
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
      {lowPools.map((pool) => (
        <ErrorText key={pool.streamId}>
          {streamLabelOf(pool.streamId)} 删除后只剩 {pool.candidates} 条候选，已低于起用下限：
          该会话的表达注入现在直接停摆，补回下限之前她不会再按表达习惯说话。
        </ErrorText>
      ))}
      {loading ? <Loading>正在读取表达方式…</Loading> : null}
      {!loading && !error && entries.length === 0 ? <Empty>没有符合条件的表达方式。</Empty> : null}
      {entries.length > 0 ? (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <Checkbox
              checked={pageAllSelected}
              onChange={toggleSelectPage}
              disabled={batchPending}
              label="全选本页"
            />
            <span className="text-sm text-muted-foreground">
              {selected.size > 0 ? `已选 ${selected.size} 条（可翻页继续选）` : '未选中任何条目'}
            </span>
            {selected.size > 0 ? (
              <Button
                variant="ghost"
                size="sm"
                disabled={batchPending}
                onClick={() => setSelected(new Set())}
              >
                清除选择
              </Button>
            ) : null}
            <Button
              variant="danger-outline"
              size="sm"
              className="ml-auto"
              disabled={batchPending || selected.size === 0}
              onClick={() => setBatchConfirm(true)}
            >
              {batchPending ? '删除中…' : `批量删除 ${selected.size} 条`}
            </Button>
          </div>
          <Card>
            <CardBody className="p-0">
              <ol>
                {entries.map((entry) => (
                  <ExpressionRow
                    key={entry.id}
                    entry={entry}
                    streamLabelOf={streamLabelOf}
                    pending={pendingId === entry.id || batchPending}
                    selected={selected.has(entry.id)}
                    onReview={review}
                    onDelete={setPendingDelete}
                    onToggleSelect={toggleSelect}
                  />
                ))}
              </ol>
            </CardBody>
          </Card>
        </>
      ) : null}

      <Pager page={page} pageCount={pageCount} total={total} onChange={setPage} disabled={loading} />

      <ConfirmDialog
        open={pendingDelete !== null}
        title="删除这条表达方式"
        description={
          <>
            <p className="font-medium">「{pendingDelete?.style}」</p>
            <p className="mt-1">
              删除不可逆。想只让它停止生效、以后也不被重新学回来，用「驳回」——
              驳回保留这一行作为记号，删除则会让同样的说法日后可以被再次学到。
            </p>
          </>
        }
        confirmText="删除"
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (pendingDelete) void removeEntry(pendingDelete)
        }}
      />

      <ConfirmDialog
        open={batchConfirm}
        title={`删除选中的 ${selected.size} 条表达方式`}
        description={
          <>
            <p>删除不可逆，且同样的说法日后可以被重新学到。要让某条永久停止生效请改用「驳回」。</p>
            <p className="mt-1">
              候选数跌破起用下限的会话会直接停止注入表达，删除后若发生会在页面上报出来。
            </p>
          </>
        }
        confirmText={`删除 ${selected.size} 条`}
        onCancel={() => setBatchConfirm(false)}
        onConfirm={() => void removeSelected()}
      />
    </div>
  )
}
