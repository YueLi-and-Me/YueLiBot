/**
 * 表情包库管理页：浏览、封禁、解封与删除，逐张与批量两条路径。
 *
 * 排序两个按钮、四种口径：点已激活的按钮翻方向。默认按时间倒序（后入库的排在
 * 最前）；把「按使用次数」翻成升序时排序与后台淘汰完全同口径（use_count 升序、
 * last_used_at 升序），此时页面里越靠前的条目就是真的会先被淘汰的条目。顶部总览
 * 给出库容量、目录占用与孤儿文件三组数字；封禁按内容哈希独立存在，删除记录或
 * 文件不会解除封禁，因此「封禁 / 解封」与「删除」是两个独立动作，各自都有批量
 * 版本。
 */
import { Ban, ImageOff, RotateCcw, Trash2 } from 'lucide-react'
import { useState } from 'react'

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
  Loading,
  Metric,
  Pager,
  SegmentedTabs,
  Progress,
  toast,
} from '@/components/ui'
import { apiMutate, UnauthorizedError } from '@/lib/api'
import { dateTime } from '@/lib/format'
import {
  listOrderField,
  listOrderTabs,
  nextListOrder,
  type ListOrder,
} from '@/lib/list-ops'
import { useAuth } from '@/hooks/use-auth'
import {
  banEmojis,
  deleteEmojis,
  unbanEmojis,
  useEmojis,
  type EmojiEntry,
  type EmojiStats,
} from '@/hooks/use-emojis'
import { useSelection } from '@/hooks/use-selection'

/** 页大小；缩略图网格偏重，一页 24 张约两屏。 */
const PAGE_SIZE = 24

/**
 * 把字节数格式化为「x.x MB / x KB」的中文文本。
 *
 * @param bytes 字节数。
 * @returns 以 1024 为底的可读文本。
 */
function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${bytes} B`
}

/**
 * 渲染单张表情包卡片。
 *
 * @param props.entry 表情记录。
 * @param props.selected 该张是否已被勾选进批量动作的选择集。
 * @param props.onBan 封禁回调。
 * @param props.onUnban 解封回调。
 * @param props.onDelete 删除回调。
 * @param props.onToggleSelect 勾选框切换时的回调，参数为内容哈希。
 * @param props.busy 本卡片是否正在执行写操作。
 * @returns 一张带缩略图与操作按钮的卡片。
 */
function EmojiCard({
  entry,
  selected,
  onBan,
  onUnban,
  onDelete,
  onToggleSelect,
  busy,
}: {
  entry: EmojiEntry
  selected: boolean
  onBan: (entry: EmojiEntry) => void
  onUnban: (entry: EmojiEntry) => void
  onDelete: (entry: EmojiEntry) => void
  onToggleSelect: (hash: string) => void
  busy: boolean
}) {
  return (
    <Card className={entry.banned ? 'ring-1 ring-destructive/40' : undefined}>
      <CardBody className="flex flex-col gap-2">
        <div className="relative aspect-square w-full overflow-hidden rounded-lg bg-muted">
          {/* 缩略图带会话 Cookie，直接同源请求；失败时显示占位图标。 */}
          <img
            src={`/api/emojis/${entry.hash}/thumbnail`}
            alt={entry.emotionTags}
            loading="lazy"
            className="size-full object-contain"
            onError={(event) => {
              event.currentTarget.style.display = 'none'
            }}
          />
          {/* 勾选框浮在图上：网格里没有行首可以放，压在角上带底衬才在深浅图上都看得见。
              z-10 是必需的：封禁横幅同为绝对定位且在后面渲染，不抬层级会把勾选框
              整个盖住，已封禁的条目就没法选进批量解封。横幅左侧同步留出让位。 */}
          <Checkbox
            checked={selected}
            onChange={() => onToggleSelect(entry.hash)}
            disabled={busy}
            className="absolute left-1.5 top-1.5 z-10 rounded-md bg-card/85 p-1 backdrop-blur-sm"
          />
          {entry.banned ? (
            <span className="absolute inset-x-0 top-0 bg-destructive/90 py-1 pl-9 pr-2 text-center text-[11px] font-semibold text-white">
              已封禁
            </span>
          ) : null}
        </div>
        <div className="flex flex-wrap gap-1.5">
          {entry.emotionTags.split(',').map((tag) => tag.trim()).filter(Boolean).map((tag) => (
            <Chip key={tag} label="标签" value={tag} />
          ))}
        </div>
        <div className="flex items-baseline justify-between gap-2 text-xs text-muted-foreground">
          <span>
            她用 <strong className="font-mono text-[13px] font-semibold text-foreground">{entry.useCount}</strong> 次
            · 见到 <strong className="font-mono text-[13px]">{entry.seenCount}</strong> 次
          </span>
        </div>
        <div className="text-[11px] text-muted-foreground">
          最后使用：{dateTime(entry.lastUsedAt)}
        </div>
        <div className="mt-auto flex gap-1.5 pt-1">
          {entry.banned ? (
            <Button variant="secondary" size="sm" disabled={busy} onClick={() => onUnban(entry)}>
              <RotateCcw className="size-3.5" aria-hidden="true" />
              解封
            </Button>
          ) : (
            <Button variant="secondary" size="sm" disabled={busy} onClick={() => onBan(entry)}>
              <Ban className="size-3.5" aria-hidden="true" />
              封禁
            </Button>
          )}
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => onDelete(entry)}>
            <Trash2 className="size-3.5" aria-hidden="true" />
            删除
          </Button>
        </div>
      </CardBody>
    </Card>
  )
}

/** 待确认的写操作：type 区分封禁 / 解封 / 删除，entry 为 null 表示作用于选择集。 */
interface PendingAction {
  type: 'ban' | 'unban' | 'delete'
  /** 单张时是目标记录；批量时为 null。 */
  entry: EmojiEntry | null
}

/** 三种动作的中文名，弹窗标题与按钮共用一份，避免两处措辞漂移。 */
const ACTION_LABEL: Record<PendingAction['type'], string> = {
  ban: '封禁',
  unban: '解封',
  delete: '删除',
}

/**
 * 渲染表情包管理页。
 *
 * @returns 页面容器元素；总览卡 + 缩略图网格 + 分页，写操作经确认弹窗执行。
 */
export function EmojisPage() {
  const { handleUnauthorized } = useAuth()
  const [page, setPage] = useState(0)
  /** 封禁筛选：all 不限 / banned 只看已封禁 / active 只看未封禁。 */
  const [scope, setScope] = useState<'all' | 'banned' | 'active'>('all')
  const [order, setOrder] = useState<ListOrder>('time_desc')
  const [refreshKey, setRefreshKey] = useState(0)
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [busy, setBusy] = useState(false)

  const { entries, total, stats, loading, error } = useEmojis({
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    banned: scope === 'all' ? null : scope === 'banned',
    order,
    refreshKey,
  })

  const selection = useSelection(entries.map((entry) => entry.hash))

  const changeFilter = (apply: () => void) => {
    apply()
    setPage(0)
    selection.clear()
  }

  const runAction = async (action: PendingAction) => {
    setBusy(true)
    try {
      if (action.entry !== null) {
        const { hash } = action.entry
        if (action.type === 'ban') {
          await apiMutate(`/api/emojis/${hash}/ban`, 'POST', { reason: '' })
          toast.success('已封禁；同一张图即使被删除也不会再入库')
        } else if (action.type === 'unban') {
          await apiMutate(`/api/emojis/${hash}/unban`, 'POST')
          toast.success('已解封')
        } else {
          await apiMutate(`/api/emojis/${hash}`, 'DELETE')
          toast.success('已删除记录与文件')
        }
      } else if (action.type === 'ban') {
        const result = await banEmojis(selection.ids)
        selection.clear()
        toast.success(`已封禁 ${result.affected} 张；这些图即使被删除也不会再入库`)
      } else if (action.type === 'unban') {
        const result = await unbanEmojis(selection.ids)
        selection.clear()
        toast.success(`已解封 ${result.affected} 张`)
      } else {
        const result = await deleteEmojis(selection.ids)
        selection.clear()
        toast.success(`已删除 ${result.affected} 张的记录与文件`)
        // 删完当前页可能整页落空，把页码夹回新的末页，避免停在空白分页上。
        const nextPageCount = Math.max(1, Math.ceil((total - result.affected) / PAGE_SIZE))
        setPage((current) => Math.min(current, nextPageCount - 1))
      }
      setRefreshKey((key) => key + 1)
    } catch (err: unknown) {
      if (err instanceof UnauthorizedError) handleUnauthorized(err)
      else toast.error(`操作失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBusy(false)
      setPending(null)
    }
  }

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const batch = pending !== null && pending.entry === null
  const actionLabel = pending ? ACTION_LABEL[pending.type] : ''

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI · CONSOLE"
        title="表情包库"
        subtitle="她能发出去的表情都在这里；把「按使用次数」翻成升序就是真会先被淘汰的顺序。"
      />
      {stats ? <StatsOverview stats={stats} /> : null}
      <div className="flex flex-wrap items-center gap-3">
        <SegmentedTabs
          tabs={[
            { value: 'all', label: '全部' },
            { value: 'active', label: '未封禁' },
            { value: 'banned', label: `已封禁${stats ? ` (${stats.bannedInLibrary})` : ''}` },
          ]}
          value={scope}
          onChange={(next) => changeFilter(() => setScope(next))}
        />
        {/* 两个按钮表达四种口径：点已激活的按钮翻方向，箭头写在激活项的文案里。 */}
        <SegmentedTabs
          tabs={listOrderTabs(order)}
          value={listOrderField(order)}
          onChange={(field) => changeFilter(() => setOrder(nextListOrder(order, field)))}
        />
      </div>
      {error ? <ErrorText>{error}</ErrorText> : null}
      {loading ? <Loading>正在读取表情包库…</Loading> : null}
      {!loading && !error && entries.length === 0 ? (
        <Empty>
          <ImageOff className="mx-auto mb-2 size-8 text-muted-foreground" aria-hidden="true" />
          {scope === 'banned' ? '还没有封禁过表情包。'
            : scope === 'active' ? '库里的表情包全部被封禁了。'
            : '库里还没有表情包。'}
        </Empty>
      ) : null}
      {entries.length > 0 ? (
        <>
          <BatchBar
            pageAllSelected={selection.pageAllSelected}
            onTogglePage={selection.togglePage}
            selectedCount={selection.size}
            onClear={selection.clear}
            busy={busy}
          >
            <Button
              variant="secondary"
              size="sm"
              disabled={busy || selection.size === 0}
              onClick={() => setPending({ type: 'ban', entry: null })}
            >
              <Ban className="size-3.5" aria-hidden="true" />
              批量封禁
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={busy || selection.size === 0}
              onClick={() => setPending({ type: 'unban', entry: null })}
            >
              <RotateCcw className="size-3.5" aria-hidden="true" />
              批量解封
            </Button>
            <Button
              variant="danger-outline"
              size="sm"
              disabled={busy || selection.size === 0}
              onClick={() => setPending({ type: 'delete', entry: null })}
            >
              <Trash2 className="size-3.5" aria-hidden="true" />
              {`批量删除 ${selection.size} 张`}
            </Button>
          </BatchBar>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
            {entries.map((entry) => (
              <EmojiCard
                key={entry.hash}
                entry={entry}
                busy={busy}
                selected={selection.selected.has(entry.hash)}
                onBan={(item) => setPending({ type: 'ban', entry: item })}
                onUnban={(item) => setPending({ type: 'unban', entry: item })}
                onDelete={(item) => setPending({ type: 'delete', entry: item })}
                onToggleSelect={selection.toggle}
              />
            ))}
          </div>
        </>
      ) : null}

      {/* 这里原本自己抄了一份翻页条；换成共用 Pager 后行为一致，且「回到第一页」
          与其余列表页保持同一个控件，不必两处各加一次。 */}
      <Pager page={page} pageCount={pageCount} total={total} onChange={setPage} disabled={loading} />

      <ConfirmDialog
        open={pending !== null}
        title={
          batch
            ? `${actionLabel}选中的 ${selection.size} 张表情`
            : pending?.type === 'ban' ? '封禁这张表情'
              : pending?.type === 'unban' ? '解除封禁'
              : '删除这张表情'
        }
        description={
          pending?.type === 'ban'
            ? '封禁按内容哈希独立保存：即使记录被淘汰或文件被删，同一张图也不会再入库。'
            : pending?.type === 'unban'
              ? '解除后这些图再次出现在聊天里时可以重新入库。'
              : '删除记录与磁盘文件；文件内容不会保留，再次遇到时需要重新识别登记。想让它永远进不来请改用封禁。'
        }
        confirmText={batch ? `${actionLabel} ${selection.size} 张` : actionLabel}
        danger={pending?.type !== 'unban'}
        onCancel={() => setPending(null)}
        onConfirm={() => {
          if (pending && !busy) void runAction(pending)
        }}
      />
    </div>
  )
}

/**
 * 渲染库容量总览卡。
 *
 * @param props.stats 后端返回的容量统计。
 * @returns 指标卡；maxCount 为 0 时不渲染容量进度条。
 */
function StatsOverview({ stats }: { stats: EmojiStats }) {
  // 容量按 countedCount 算：已封禁的记录不占名额（服务端 evict_to_limit 同口径），
  // 用 count 会让容量条比真实占用虚高，看着快满了其实还早。
  const capacityPercent =
    stats.maxCount > 0 ? Math.min(100, (stats.countedCount / stats.maxCount) * 100) : 0
  return (
    <Card>
      <CardBody className="grid grid-cols-2 gap-x-6 gap-y-1 md:grid-cols-4">
        <Metric
          label="库内记录"
          value={stats.count}
          detail={stats.bannedInLibrary > 0 ? `其中 ${stats.bannedInLibrary} 条已封禁，不占容量` : '全部计入容量'}
        />
        <Metric
          label="封禁哈希"
          value={stats.bannedCount}
          detail={stats.bannedCount > stats.bannedInLibrary
            ? `${stats.bannedCount - stats.bannedInLibrary} 条对应的图已不在库里`
            : '独立于记录存在'}
        />
        <Metric label="目录占用" value={formatBytes(stats.directoryBytes)} detail={`${stats.fileCount} 个文件`} />
        <Metric
          label="孤儿文件"
          value={stats.orphanCount}
          detail={formatBytes(stats.orphanBytes)}
        />
        {stats.maxCount > 0 ? (
          <div className="col-span-2 md:col-span-4">
            <div className="mb-1 flex items-center justify-between text-xs text-muted-foreground">
              <span>库容量{stats.bannedInLibrary > 0 ? '（不含已封禁）' : ''}</span>
              <span className="font-mono">{stats.countedCount} / {stats.maxCount}</span>
            </div>
            <Progress value={capacityPercent} max={100} label="表情包库容量" />
          </div>
        ) : null}
        {/* use_count / last_used_at 两列自 2026-08-25 才开始落账，库里更早的发送
            天然记为 0；不说明的话，「她发过很多次」和卡片上的小数字对不上。 */}
        <p className="col-span-2 md:col-span-4 text-xs text-muted-foreground">
          使用次数自 2026-08-25 起统计，此前的发送不计入。
        </p>
      </CardBody>
    </Card>
  )
}
