/**
 * 表达方式的数据加载 hook。
 *
 * 对应后端 `/api/expressions` 只读接口：按会话过滤，使用次数升/降序，
 * offset 分页。这些表达来自历史迁移、尚未接入回复生成，页面负责如实标注。
 */
import { useEffect, useState } from 'react'

import { apiFetch, UnauthorizedError } from '@/lib/api'
import { useAuth } from './use-auth'

/** 单条表达方式，字段与后端响应一一对应。 */
export interface ExpressionEntry {
  id: number
  /** 使用情境描述。 */
  situation: string
  /** 具体的表达文本。 */
  style: string
  streamId: number | null
  useCount: number
  source: string
  createdAt: number
}

/** 一组过滤、排序与分页条件。 */
export interface ExpressionQuery {
  streamId: number | null
  order: 'use_desc' | 'use_asc'
  limit: number
  offset: number
}

/** useExpressions 返回的状态。 */
interface ExpressionsState {
  entries: ExpressionEntry[]
  total: number
  loading: boolean
  error: string
}

/**
 * 按查询条件读取一页表达方式。
 *
 * @param query 过滤、排序与分页条件。
 * @returns 表达列表、总数、加载态与错误文本。
 */
export function useExpressions(query: ExpressionQuery): ExpressionsState {
  const { handleUnauthorized } = useAuth()
  const [entries, setEntries] = useState<ExpressionEntry[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const { streamId, order, limit, offset } = query

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const params = new URLSearchParams({
      order,
      limit: String(limit),
      offset: String(offset),
    })
    if (streamId !== null) params.set('streamId', String(streamId))
    apiFetch<{ entries: ExpressionEntry[]; total: number }>(`/api/expressions?${params.toString()}`)
      .then((payload) => {
        if (cancelled) return
        setEntries(payload.entries)
        setTotal(payload.total)
        setError('')
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof UnauthorizedError) handleUnauthorized(err)
        else setError(`表达方式请求失败：${err instanceof Error ? err.message : String(err)}`)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [streamId, order, limit, offset, handleUnauthorized])

  return { entries, total, loading, error }
}
