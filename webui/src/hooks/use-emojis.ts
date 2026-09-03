/**
 * 表情包库管理页的数据加载 hook 与封禁/删除写操作。
 *
 * 对应后端 /api/emojis 只读列表接口以及 /api/emojis/{hash}/ban、/unban、
 * DELETE 三个逐条写接口和 batch-ban、batch-unban、batch-delete 三个批量写接口；
 * 写操作由页面调用后在本地自增刷新键重新拉取。
 */
import { useEffect, useState } from 'react'

import { apiFetch, apiMutate, UnauthorizedError } from '@/lib/api'
import { inChunks, type ListOrder } from '@/lib/list-ops'
import { useAuth } from './use-auth'

/** 单条表情包记录，字段与后端响应一一对应。 */
export interface EmojiEntry {
  hash: string
  sendRef: string
  emotionTags: string
  subType: number
  seenCount: number
  useCount: number
  lastUsedAt: number | null
  banned: boolean
}

/** 库容量与磁盘占用总览。 */
export interface EmojiStats {
  /** emoji 行总数，含已封禁的。 */
  count: number
  /** 封禁表的行数；封禁独立于 emoji 行存在，可能大于 bannedInLibrary。 */
  bannedCount: number
  /** 既在库里、又被封禁的条数。 */
  bannedInLibrary: number
  /** 计入容量上限的条数（count 减去已封禁的）；容量条用这个数。 */
  countedCount: number
  maxCount: number
  fileCount: number
  directoryBytes: number
  orphanCount: number
  orphanBytes: number
}

/** 分页、筛选与排序条件。 */
export interface EmojiQuery {
  limit: number
  offset: number
  /** 封禁筛选：true 只看已封禁，false 只看未封禁，null 不筛选。 */
  banned: boolean | null
  /** 排序口径；`use_asc` 与后台淘汰同口径，此时页面顺序即淘汰顺序。 */
  order: ListOrder
  /** 写操作后自增，触发重新拉取。 */
  refreshKey: number
}

/** useEmojis 返回的状态。 */
interface EmojisState {
  entries: EmojiEntry[]
  total: number
  stats: EmojiStats | null
  loading: boolean
  error: string
}

/**
 * 按分页与排序条件读取一页表情包与容量总览。
 *
 * @param query 分页、筛选、排序条件与刷新键。
 * @returns 表情列表、总数、总览、加载态与错误文本。
 */
export function useEmojis(query: EmojiQuery): EmojisState {
  const { handleUnauthorized } = useAuth()
  const [entries, setEntries] = useState<EmojiEntry[]>([])
  const [total, setTotal] = useState(0)
  const [stats, setStats] = useState<EmojiStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const { limit, offset, banned, order, refreshKey } = query

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
      order,
    })
    if (banned !== null) params.set('banned', String(banned))
    apiFetch<{ entries: EmojiEntry[]; total: number; stats: EmojiStats }>(
      `/api/emojis?${params.toString()}`,
    )
      .then((payload) => {
        if (cancelled) return
        setEntries(payload.entries)
        setTotal(payload.total)
        setStats(payload.stats)
        setError('')
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof UnauthorizedError) handleUnauthorized(err)
        else setError(`表情包列表请求失败：${err instanceof Error ? err.message : String(err)}`)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [limit, offset, banned, order, refreshKey, handleUnauthorized])

  return { entries, total, stats, loading, error }
}

/** 批量写操作的结果：实际生效条数与请求条数。 */
export interface EmojiBatchResult {
  affected: number
  requested: number
}

/**
 * 批量封禁表情包，超过单次上限时自动分批。
 *
 * 封禁按内容哈希独立保存：记录被淘汰或文件被删之后封禁依然生效，同一张图不会
 * 再入库。已封禁的条目不重复计数，因此 `affected` 可能小于 `requested`。
 *
 * @param hashes 待封禁的内容哈希列表；不限长度，内部按上限分批发出。
 * @param reason 可选封禁原因，整批共用。
 * @returns 新增封禁条数与请求条数。
 * @throws UnauthorizedError 会话失效时抛出；其余错误原样传播，由调用方展示。
 */
export async function banEmojis(hashes: string[], reason = ''): Promise<EmojiBatchResult> {
  const results = await inChunks(hashes, (chunk) =>
    apiMutate<{ banned: number }>('/api/emojis/batch-ban', 'POST', { hashes: chunk, reason }),
  )
  return {
    affected: results.reduce((sum, item) => sum + item.banned, 0),
    requested: hashes.length,
  }
}

/**
 * 批量解除封禁，超过单次上限时自动分批。
 *
 * @param hashes 待解封的内容哈希列表。
 * @returns 实际解封条数与请求条数。
 * @throws UnauthorizedError 会话失效时抛出；其余错误原样传播，由调用方展示。
 */
export async function unbanEmojis(hashes: string[]): Promise<EmojiBatchResult> {
  const results = await inChunks(hashes, (chunk) =>
    apiMutate<{ unbanned: number }>('/api/emojis/batch-unban', 'POST', { hashes: chunk }),
  )
  return {
    affected: results.reduce((sum, item) => sum + item.unbanned, 0),
    requested: hashes.length,
  }
}

/**
 * 批量删除记录与磁盘文件，超过单次上限时自动分批。
 *
 * 与封禁的分工：删除只清掉这一份记录与文件，同一张图再次出现在聊天里仍会重新
 * 入库；要让它永远进不来必须封禁。
 *
 * @param hashes 待删除的内容哈希列表；库中不存在的条目静默跳过。
 * @returns 实际删除条数与请求条数。
 * @throws UnauthorizedError 会话失效时抛出；其余错误原样传播，由调用方展示。
 */
export async function deleteEmojis(hashes: string[]): Promise<EmojiBatchResult> {
  const results = await inChunks(hashes, (chunk) =>
    apiMutate<{ removed: number }>('/api/emojis/batch-delete', 'POST', { hashes: chunk }),
  )
  return {
    affected: results.reduce((sum, item) => sum + item.removed, 0),
    requested: hashes.length,
  }
}
