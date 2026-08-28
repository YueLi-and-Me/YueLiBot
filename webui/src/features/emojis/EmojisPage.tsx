/**
 * 表情包库管理页：浏览、封禁、解封与手动删除。
 *
 * 列表排序与后台淘汰完全同口径（use_count 升序、last_used_at 升序）：
 * 页面里越靠前的条目就是真的会先被淘汰的条目。顶部总览给出库容量、
 * 目录占用与孤儿文件三组数字；封禁按内容哈希独立存在，删除记录或文件
 * 不会解除封禁，因此行内用「封禁 / 解封」与「删除」两个独立动作。
 */
import { Ban, ImageOff, RotateCcw, Trash2 } from 'lucide-react'
import { useState } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import {
  Button,
  Card,
  CardBody,
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
import { useAuth } from '@/hooks/use-auth'
import { useEmojis, type EmojiEntry, type EmojiStats } from '@/hooks/use-emojis'

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
 * @param props.onBan 封禁回调。
 * @param props.onUnban 解封回调。
 * @param props.onDelete 删除回调。
 * @param props.busy 本卡片是否正在执行写操作。
 * @returns 一张带缩略图与操作按钮的卡片。
 */
function EmojiCard({
  entry,
  onBan,
  onUnban,
  onDelete,
  busy,
}: {
  entry: EmojiEntry
  onBan: (entry: EmojiEntry) => void
  onUnban: (entry: EmojiEntry) => void
  onDelete: (entry: EmojiEntry) => void
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
          {entry.banned ? (
            <span className="absolute inset-x-0 top-0 bg-destructive/90 px-2 py-1 text-center text-[11px] font-semibold text-white">
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

/** 待确认的写操作：type 区分封禁 / 解封 / 删除。 */
interface PendingAction {
  type: 'ban' | 'unban' | 'delete'
  entry: EmojiEntry
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
  const [refreshKey, setRefreshKey] = useState(0)
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [busy, setBusy] = useState(false)

  const { entries, total, stats, loading, error } = useEmojis({
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    banned: scope === 'all' ? null : scope === 'banned',
    refreshKey,
  })

  const runAction = async (action: PendingAction) => {
    setBusy(true)
    try {
      if (action.type === 'ban') {
        await apiMutate(`/api/emojis/${action.entry.hash}/ban`, 'POST', { reason: '' })
        toast.success('已封禁；同一张图即使被删除也不会再入库')
      } else if (action.type === 'unban') {
        await apiMutate(`/api/emojis/${action.entry.hash}/unban`, 'POST')
        toast.success('已解封')
      } else {
        await apiMutate(`/api/emojis/${action.entry.hash}`, 'DELETE')
        toast.success('已删除记录与文件')
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

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI · CONSOLE"
        title="表情包库"
        subtitle="她能发出去的表情都在这里；列表顺序就是真会先被淘汰的顺序。"
      />
      {stats ? <StatsOverview stats={stats} /> : null}
      <SegmentedTabs
        tabs={[
          { value: 'all', label: '全部' },
          { value: 'active', label: '未封禁' },
          { value: 'banned', label: `已封禁${stats ? ` (${stats.bannedInLibrary})` : ''}` },
        ]}
        value={scope}
        onChange={(next) => {
          setScope(next)
          setPage(0)
        }}
      />
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
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
          {entries.map((entry) => (
            <EmojiCard
              key={entry.hash}
              entry={entry}
              busy={busy}
              onBan={(item) => setPending({ type: 'ban', entry: item })}
              onUnban={(item) => setPending({ type: 'unban', entry: item })}
              onDelete={(item) => setPending({ type: 'delete', entry: item })}
            />
          ))}
        </div>
      ) : null}

      {/* 这里原本自己抄了一份翻页条；换成共用 Pager 后行为一致，且「回到第一页」
          与其余列表页保持同一个控件，不必两处各加一次。 */}
      <Pager page={page} pageCount={pageCount} total={total} onChange={setPage} disabled={loading} />

      <ConfirmDialog
        open={pending !== null}
        title={
          pending?.type === 'ban' ? '封禁这张表情'
          : pending?.type === 'unban' ? '解除封禁'
          : '删除这张表情'
        }
        description={
          pending?.type === 'ban'
            ? '封禁按内容哈希独立保存：即使记录被淘汰或文件被删，同一张图也不会再入库。'
            : pending?.type === 'unban'
              ? '解除后这张图再次出现在聊天里时可以重新入库。'
              : '删除记录与磁盘文件；文件内容不会保留，再次遇到时需要重新识别登记。'
        }
        confirmText={
          pending?.type === 'ban' ? '封禁' : pending?.type === 'unban' ? '解封' : '删除'
        }
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
      </CardBody>
    </Card>
  )
}
