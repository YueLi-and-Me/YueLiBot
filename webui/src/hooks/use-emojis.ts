/**
 * 表情包库管理页的数据加载 hook。
 *
 * 对应后端 /api/emojis 只读列表接口（与后台淘汰同口径排序）以及
 * /api/emojis/{hash}/ban、/unban、DELETE 三个写接口；写操作由页面调用
 * apiMutate 后在本地刷新页码键重新拉取。
 */
import { useEffect, useState } from 'react'

import { apiFetch, UnauthorizedError } from '@/lib/api'
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
  count: number
  bannedCount: number
  maxCount: number
  fileCount: number
  directoryBytes: number
  orphanCount: number
  orphanBytes: number
}

/** 分页条件。 */
export interface EmojiQuery {
  limit: number
  offset: number
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
 * 按分页条件读取一页表情包与容量总览。
 *
 * @param query 分页条件与刷新键。
 * @returns 表情列表、总数、总览、加载态与错误文本。
 */
export function useEmojis(query: EmojiQuery): EmojisState {
  const { handleUnauthorized } = useAuth()
  const [entries, setEntries] = useState<EmojiEntry[]>([])
  const [total, setTotal] = useState(0)
  const [stats, setStats] = useState<EmojiStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const { limit, offset, refreshKey } = query

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })
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
  }, [limit, offset, refreshKey, handleUnauthorized])

  return { entries, total, stats, loading, error }
}
