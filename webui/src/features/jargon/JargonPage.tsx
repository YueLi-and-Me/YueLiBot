/**
 * 黑话词表页：浏览词条并做人工复核。
 *
 * 列表形态（一人一行同款理由：信息密度优先于卡片网格）。一眼要能看出
 * 「这条是全局的还是只在某个群成立」：词条行内用范围徽标区分全局与会话。
 * 默认只显示已确认词条；待定候选与人工驳回的条目需显式切页签——待定既来自迁移
 * 时的低置信条目，也来自名字守卫把「疑似人名」批量降级的结果，因此不为空。
 * 关键词输入采用提交式（回车或点按钮），避免逐键请求。
 *
 * 「在用」与「在学」分开表述，同表达方式页：召回已接入 planner 与 replyer，
 * 学习服务在后台累积证据并按阶梯阈值推断。待定页签里区分「待判定」（尚未
 * 攒够证据）与「判定为普通词」（推断过、群内用法与通用含义一致），行内
 * 显示学习期出现次数。
 *
 * 自动判定之上还有一层人工复核，三种动作都同时提供行内与批量两条路径：
 * - 确认：判定「这是黑话」，进入注入；
 * - 驳回：判定「这不是黑话」，退出注入。驳回与确认都会**锁定推断阶梯**，
 *   否则证据继续增长时自动判定会把人工结论覆盖回去；驳回保留行，学习器再遇到
 *   同一个词只累加证据、不会重新插入。撤销驳回则解锁，把词交回自动判定。
 * - 删除：不可逆，同一个词日后会作为全新候选重新入库、从待定重走一遍判定。
 *   清理误抽取的噪声用删除，压制一个真实存在但不该注入的词用驳回。
 *
 * 默认按入库时间倒序，后学到的排在最前；另一个按钮按查表命中次数排，
 * 两个按钮都可以再点一次翻方向。
 */
import { Search, X } from 'lucide-react'
import { useState, type FormEvent } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import {
  BatchBar,
  Button,
  Card,
  CardBody,
  Checkbox,
  Chip,
  ConfirmDialog,
  Empty,
  ErrorText,
  Field,
  Input,
  Loading,
  Pager,
  Select,
  SegmentedTabs,
  cn,
  toast,
} from '@/components/ui'
import { useAuth } from '@/hooks/use-auth'
import {
  deleteJargon,
  deleteJargonEntries,
  setJargonStatus,
  setJargonStatuses,
  useJargon,
  type JargonEntry,
  type JargonStatus,
} from '@/hooks/use-jargon'
import { useSelection } from '@/hooks/use-selection'
import { useStreams } from '@/hooks/use-observability'
import { UnauthorizedError } from '@/lib/api'
import { dateTime, streamLabel } from '@/lib/format'
import {
  listOrderField,
  listOrderTabs,
  nextListOrder,
  type ListOrder,
} from '@/lib/list-ops'

/** 页大小；一屏多一点为宜，太长要一直滚。后端路由 le=200，取值留足余量。 */
const PAGE_SIZE = 20

/**
 * 渲染单条黑话词条行。
 *
 * @param props.entry 词条数据。
 * @param props.streamLabelOf 按 streamId 取会话标签的函数。
 * @param props.pending 该行是否有写入在飞。
 * @param props.selected 该行是否已被勾选进批量动作的选择集。
 * @param props.onReview 点击确认/驳回/撤销驳回时的回调。
 * @param props.onDelete 点击删除时的回调；由调用方弹确认框，本组件只发起。
 * @param props.onToggleSelect 勾选框切换时的回调，参数为行 ID。
 * @returns 一行词条元素。
 */
function JargonRow({
  entry,
  streamLabelOf,
  pending,
  selected,
  onReview,
  onDelete,
  onToggleSelect,
}: {
  entry: JargonEntry
  streamLabelOf: (id: number) => string
  pending: boolean
  selected: boolean
  onReview: (id: number, status: JargonStatus) => void
  onDelete: (entry: JargonEntry) => void
  onToggleSelect: (id: number) => void
}) {
  const rejected = entry.status === 'rejected'
  return (
    <li
      className={cn(
        'flex flex-col gap-1.5 border-b border-border/50 px-4 py-3 last:border-b-0',
        rejected && 'opacity-55',
      )}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {/* 勾选框在行首，只喂批量动作；行内确认/驳回/删除与选择集互不影响。 */}
        <Checkbox checked={selected} onChange={() => onToggleSelect(entry.id)} disabled={pending} />
        <span className={cn('text-[15px] font-semibold', rejected && 'line-through')}>
          「{entry.term}」
        </span>
        <Chip
          label="范围"
          value={entry.streamId === null ? '全局通用' : streamLabelOf(entry.streamId)}
        />
        {/* hits 记的是查表命中，含被跨轮去重排除、被条数上限截掉、
            最终没进提示词的那些；它不等于「注进去过几次」。 */}
        <Chip label="命中" value={`${entry.hits} 次`} />
        {/* sightings 是学习期证据累计（每批语料每词至多 +1），阶梯阈值
            判据；与 hits 语义不同。 */}
        <Chip label="出现" value={`${entry.sightings} 次`} />
        <Chip label="入库" value={dateTime(entry.createdAt)} />
        {entry.status === 'pending' ? (
          /* pending 且从未推断过是「待判定」候选；推断过仍是 pending，
             说明三步比较认定它是普通词，回 pending 只存不用。 */
          <Chip
            label="判定"
            value={entry.inferredAtSightings === 0 ? '待判定' : '判定为普通词'}
          />
        ) : null}
        {rejected ? <Chip label="复核" value="已驳回 · 不再注入" /> : null}
        <span className="ml-auto font-mono text-[11px] text-muted-foreground">{entry.source}</span>
        <Button
          variant="secondary"
          size="sm"
          disabled={pending || entry.status === 'confirmed'}
          onClick={() => onReview(entry.id, 'confirmed')}
        >
          确认
        </Button>
        {/* 同一个位置在已驳回条目上变成撤销：驳回是可逆的，撤销把词交回自动判定。 */}
        {rejected ? (
          <Button
            variant="secondary"
            size="sm"
            disabled={pending}
            onClick={() => onReview(entry.id, 'pending')}
          >
            撤销驳回
          </Button>
        ) : (
          <Button
            variant="danger-outline"
            size="sm"
            disabled={pending}
            onClick={() => onReview(entry.id, 'rejected')}
          >
            驳回
          </Button>
        )}
        {/* 行内删除取 ghost：与表情包页同惯例，危险性由确认弹窗承担，
            按钮本身不抢视线。 */}
        <Button variant="ghost" size="sm" disabled={pending} onClick={() => onDelete(entry)}>
          删除
        </Button>
      </div>
      {/* 释义普遍是长段落，默认收两行，完整内容悬停可见。 */}
      <p className="line-clamp-2 text-sm leading-relaxed text-muted-foreground" title={entry.meaning}>
        {entry.meaning}
      </p>
    </li>
  )
}

/**
 * 渲染黑话词表页。
 *
 * @returns 页面容器元素；筛选变化自动回到第一页并清空选择集。
 */
export function JargonPage() {
  const { handleUnauthorized } = useAuth()
  const { streams, error: streamsError } = useStreams()
  const [status, setStatus] = useState<JargonStatus>('confirmed')
  /** all=全部；global=仅全局；其余为具体会话 ID。 */
  const [scope, setScope] = useState('all')
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [order, setOrder] = useState<ListOrder>('time_desc')
  const [page, setPage] = useState(0)
  /** 复核或删除写入后递增，触发列表重新拉取。 */
  const [refreshKey, setRefreshKey] = useState(0)
  /** 正在写入的行 id；在飞期间禁用该行全部动作。 */
  const [pendingId, setPendingId] = useState<number | null>(null)
  const [writeError, setWriteError] = useState('')
  /** 待确认删除的行；null 表示确认弹窗关闭。删除不可逆，必须过一道确认。 */
  const [pendingDelete, setPendingDelete] = useState<JargonEntry | null>(null)
  /** 批量删除确认弹窗开关。 */
  const [batchConfirm, setBatchConfirm] = useState(false)
  /** 批量写入在飞；期间禁用整条工具条。 */
  const [batchPending, setBatchPending] = useState(false)

  const streamId = scope !== 'all' && scope !== 'global' ? Number(scope) : null
  const { entries, total, loading, error } = useJargon({
    status,
    streamId,
    globalOnly: scope === 'global',
    keyword,
    order,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    refreshKey,
  })

  const selection = useSelection(entries.map((entry) => entry.id))

  const streamLabelOf = (id: number) => {
    const stream = streams.find((item) => item.id === id)
    return stream ? streamLabel(stream) : `会话 #${id}`
  }

  const changeFilter = (apply: () => void) => {
    apply()
    setPage(0)
    selection.clear()
  }

  const submitKeyword = (event: FormEvent) => {
    event.preventDefault()
    changeFilter(() => setKeyword(keywordInput.trim()))
  }

  const review = async (id: number, next: JargonStatus) => {
    setPendingId(id)
    setWriteError('')
    try {
      await setJargonStatus(id, next)
      setRefreshKey((key) => key + 1)
    } catch (err: unknown) {
      if (err instanceof UnauthorizedError) handleUnauthorized(err)
      else setWriteError(`复核写入失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setPendingId(null)
    }
  }

  const removeEntry = async (entry: JargonEntry) => {
    setPendingDelete(null)
    setPendingId(entry.id)
    setWriteError('')
    try {
      await deleteJargon(entry.id)
      setRefreshKey((key) => key + 1)
    } catch (err: unknown) {
      if (err instanceof UnauthorizedError) handleUnauthorized(err)
      else setWriteError(`删除失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setPendingId(null)
    }
  }

  const reviewSelected = async (next: JargonStatus) => {
    setBatchPending(true)
    setWriteError('')
    try {
      const result = await setJargonStatuses(selection.ids, next)
      selection.clear()
      toast.success(
        next === 'confirmed'
          ? `已确认 ${result.affected} 条；这些词进入注入且不再被自动判定改写`
          : next === 'rejected'
            ? `已驳回 ${result.affected} 条；它们退出注入，学习器不会再把它们判回来`
            : `已撤销 ${result.affected} 条驳回；这些词交回自动判定`,
      )
      setRefreshKey((key) => key + 1)
    } catch (err: unknown) {
      if (err instanceof UnauthorizedError) handleUnauthorized(err)
      else setWriteError(`批量复核失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBatchPending(false)
    }
  }

  const removeSelected = async () => {
    setBatchConfirm(false)
    setBatchPending(true)
    setWriteError('')
    try {
      const result = await deleteJargonEntries(selection.ids)
      selection.clear()
      toast.success(`已删除 ${result.affected} 条词条`)
      // 删完当前页可能整页落空，把页码夹回新的末页，避免停在空白分页上。
      const nextPageCount = Math.max(1, Math.ceil((total - result.affected) / PAGE_SIZE))
      setPage((current) => Math.min(current, nextPageCount - 1))
      setRefreshKey((key) => key + 1)
    } catch (err: unknown) {
      if (err instanceof UnauthorizedError) handleUnauthorized(err)
      else setWriteError(`批量删除失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBatchPending(false)
    }
  }

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const viewingRejected = status === 'rejected'

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI · CONSOLE"
        title="黑话词表"
        subtitle="只有他们才懂的说法与含义，默认后学到的排在最前。"
      />
      <div className="flex flex-wrap items-end gap-3">
        <SegmentedTabs
          tabs={[
            { value: 'confirmed', label: '已确认' },
            { value: 'pending', label: '待定' },
            { value: 'rejected', label: '已驳回' },
          ]}
          value={status}
          onChange={(next) => changeFilter(() => setStatus(next))}
        />
        {/* 两个按钮表达四种口径：点已激活的按钮翻方向，箭头写在激活项的文案里。 */}
        <SegmentedTabs
          tabs={listOrderTabs(order)}
          value={listOrderField(order)}
          onChange={(field) => changeFilter(() => setOrder(nextListOrder(order, field)))}
        />
        <Field label="范围" htmlFor="jargon-scope" className="w-64">
          <Select
            id="jargon-scope"
            value={scope}
            onChange={(event) => changeFilter(() => setScope(event.target.value))}
          >
            <option value="all">全部词条</option>
            <option value="global">仅全局通用</option>
            {streams.map((stream) => (
              <option key={stream.id} value={stream.id}>
                仅 {streamLabel(stream)}
              </option>
            ))}
          </Select>
        </Field>
        <form className="flex items-end gap-2" onSubmit={submitKeyword}>
          <Field label="关键词" htmlFor="jargon-keyword" className="w-56">
            <Input
              id="jargon-keyword"
              value={keywordInput}
              placeholder="匹配词条或释义"
              onChange={(event) => setKeywordInput(event.target.value)}
            />
          </Field>
          <Button type="submit" variant="secondary" disabled={loading}>
            <Search className="size-4" aria-hidden="true" />
            搜索
          </Button>
          {keyword ? (
            <Button
              type="button"
              variant="secondary"
              onClick={() => {
                setKeywordInput('')
                changeFilter(() => setKeyword(''))
              }}
            >
              <X className="size-4" aria-hidden="true" />
              清除
            </Button>
          ) : null}
        </form>
      </div>

      {streamsError ? <ErrorText>{streamsError}</ErrorText> : null}
      {error ? <ErrorText>{error}</ErrorText> : null}
      {writeError ? <ErrorText>{writeError}</ErrorText> : null}
      {loading ? <Loading>正在读取黑话词表…</Loading> : null}
      {!loading && !error && entries.length === 0 ? (
        <Empty>
          {status === 'pending'
            ? '没有待定词条——既没有等待判定的候选，也没有被判定为普通词的条目。'
            : status === 'rejected'
              ? '还没有驳回过词条。'
              : '没有符合条件的词条。'}
        </Empty>
      ) : null}
      {entries.length > 0 ? (
        <>
          <BatchBar
            pageAllSelected={selection.pageAllSelected}
            onTogglePage={selection.togglePage}
            selectedCount={selection.size}
            onClear={selection.clear}
            busy={batchPending}
          >
            <Button
              variant="secondary"
              size="sm"
              disabled={batchPending || selection.size === 0}
              onClick={() => void reviewSelected('confirmed')}
            >
              批量确认
            </Button>
            {/* 已驳回页签上，同一个位置变成批量撤销：那里再点「驳回」没有意义。 */}
            <Button
              variant={viewingRejected ? 'secondary' : 'danger-outline'}
              size="sm"
              disabled={batchPending || selection.size === 0}
              onClick={() => void reviewSelected(viewingRejected ? 'pending' : 'rejected')}
            >
              {viewingRejected ? '批量撤销驳回' : '批量驳回'}
            </Button>
            <Button
              variant="danger-outline"
              size="sm"
              disabled={batchPending || selection.size === 0}
              onClick={() => setBatchConfirm(true)}
            >
              {`批量删除 ${selection.size} 条`}
            </Button>
          </BatchBar>
          <Card>
            <CardBody className="p-0">
              <ol>
                {entries.map((entry) => (
                  <JargonRow
                    key={entry.id}
                    entry={entry}
                    streamLabelOf={streamLabelOf}
                    pending={pendingId === entry.id || batchPending}
                    selected={selection.selected.has(entry.id)}
                    onReview={review}
                    onDelete={setPendingDelete}
                    onToggleSelect={selection.toggle}
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
        title="删除这条词条"
        description={
          <>
            <p className="font-medium">「{pendingDelete?.term}」</p>
            <p className="mt-1">
              删除不可逆，同一个词日后会作为全新候选重新入库、从待定重走一遍判定。
              想让它永久停止注入且不被判回来，用「驳回」——驳回保留这一行作为记号。
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
        title={`删除选中的 ${selection.size} 条词条`}
        description={
          <p>
            删除不可逆，这些词日后可以被重新学回来。要让它们永久停止注入请改用
            「批量驳回」——驳回保留行并锁住自动判定。
          </p>
        }
        confirmText={`删除 ${selection.size} 条`}
        onCancel={() => setBatchConfirm(false)}
        onConfirm={() => void removeSelected()}
      />
    </div>
  )
}
