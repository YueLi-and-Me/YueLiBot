/**
 * 黑话词表的数据加载 hook。
 *
 * 对应后端 `/api/jargon` 只读接口：按状态、会话、是否全局与关键词过滤，
 * offset 分页。查询参数对象变化即重新读取；关键词防抖由页面侧用提交式
 * 输入框承担，这里不做时间防抖。
 */
import { useEffect, useState } from 'react'

import { apiFetch, UnauthorizedError } from '@/lib/api'
import { useAuth } from './use-auth'

/** 单条黑话词条，字段与后端响应一一对应。 */
export interface JargonEntry {
  id: number
  term: string
  meaning: string
  /** 所属会话 ID；`null` 表示全局通用。 */
  streamId: number | null
  status: string
  hits: number
  source: string
  createdAt: number
}

/** 一组过滤与分页条件。 */
export interface JargonQuery {
  status: 'confirmed' | 'pending'
  streamId: number | null
  globalOnly: boolean
  keyword: string
  limit: number
  offset: number
}

/** useJargon 返回的状态。 */
interface JargonState {
  entries: JargonEntry[]
  /** 符合当前过滤条件的总条数，用于翻页与总数展示。 */
  total: number
  loading: boolean
  error: string
}

/**
 * 按查询条件读取一页黑话词条。
 *
 * @param query 过滤与分页条件。
 * @returns 词条列表、总数、加载态与错误文本。
 */
export function useJargon(query: JargonQuery): JargonState {
  const { handleUnauthorized } = useAuth()
  const [entries, setEntries] = useState<JargonEntry[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const { status, streamId, globalOnly, keyword, limit, offset } = query

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const params = new URLSearchParams({
      status,
      limit: String(limit),
      offset: String(offset),
    })
    if (streamId !== null) params.set('streamId', String(streamId))
    if (globalOnly) params.set('globalOnly', 'true')
    if (keyword) params.set('keyword', keyword)
    apiFetch<{ entries: JargonEntry[]; total: number }>(`/api/jargon?${params.toString()}`)
      .then((payload) => {
        if (cancelled) return
        setEntries(payload.entries)
        setTotal(payload.total)
        setError('')
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof UnauthorizedError) handleUnauthorized(err)
        else setError(`黑话词表请求失败：${err instanceof Error ? err.message : String(err)}`)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [status, streamId, globalOnly, keyword, limit, offset, handleUnauthorized])

  return { entries, total, loading, error }
}
