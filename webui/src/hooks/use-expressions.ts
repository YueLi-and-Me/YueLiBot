/**
 * 表达方式的数据加载 hook 与人工复核写操作。
 *
 * 对应后端 `/api/expressions` 只读接口：按会话与复核状态过滤，使用次数
 * 升/降序，offset 分页。词表由回合收尾处的后台学习增补（source 为
 * 「本机学习」），复核状态 ``checked`` 由 `setExpressionChecked` 写入：
 * 确认（1）永不自动淘汰，驳回（-1）退出候选池，未复核（0）照常可用——
 * 复核不是使用的前置条件，它的职责是剔除与保护。
 */
import { useEffect, useState } from 'react'

import { apiFetch, apiMutate, UnauthorizedError } from '@/lib/api'
import { useAuth } from './use-auth'

/** 复核状态：0 未复核，1 已确认，-1 已驳回。 */
export type ExpressionChecked = -1 | 0 | 1

/** 单条表达方式，字段与后端响应一一对应。 */
export interface ExpressionEntry {
  id: number
  /** 使用情境描述。 */
  situation: string
  /** 具体的表达文本。 */
  style: string
  streamId: number | null
  /** 累计被选中次数；含历史迁移带入的存量，不代表本部署用过。 */
  useCount: number
  source: string
  createdAt: number
  /** 最近一次被选中的毫秒时间戳；从未被选中为 null。 */
  lastUsedAt: number | null
  /** 人工复核状态。 */
  checked: ExpressionChecked
}

/** 一组过滤、排序与分页条件。 */
export interface ExpressionQuery {
  streamId: number | null
  /** 复核状态过滤；null 表示不限。 */
  checked: ExpressionChecked | null
  order: 'use_desc' | 'use_asc'
  limit: number
  offset: number
  /** 复核写操作后递增以触发重新拉取。 */
  refreshKey?: number
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

  const { streamId, checked, order, limit, offset, refreshKey } = query

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const params = new URLSearchParams({
      order,
      limit: String(limit),
      offset: String(offset),
    })
    if (streamId !== null) params.set('streamId', String(streamId))
    if (checked !== null) params.set('checked', String(checked))
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
  }, [streamId, checked, order, limit, offset, refreshKey, handleUnauthorized])

  return { entries, total, loading, error }
}

/**
 * 写一条表达方式的人工复核状态。
 *
 * @param id 表达方式行 ID。
 * @param checked 目标复核状态：1 确认（永不自动淘汰），-1 驳回（退出候选池），
 *   0 撤销复核回到未复核。
 * @returns 后端回写的实际状态。
 * @throws UnauthorizedError 会话失效时抛出；其余错误原样传播，由调用方展示。
 */
export async function setExpressionChecked(
  id: number,
  checked: ExpressionChecked,
): Promise<{ id: number; checked: ExpressionChecked }> {
  return apiMutate(`/api/expressions/${id}/checked`, 'PUT', { checked })
}

/**
 * 删除一条表达方式。
 *
 * 与驳回的分工：驳回是可逆记号，行还在、只是退出候选池，学习器再学到同样的
 * 说法时不会重复插入；删除不可逆，同样的说法以后可以被重新学回来。清理迁移
 * 存量用删除——后台淘汰的范围只有本机学习产出，存量一条都不自动删。
 *
 * @param id 表达方式行 ID。
 * @returns 被删除的 ID。
 * @throws UnauthorizedError 会话失效时抛出；其余错误原样传播，由调用方展示。
 */
export async function deleteExpression(id: number): Promise<{ id: number }> {
  return apiMutate(`/api/expressions/${id}`, 'DELETE')
}
