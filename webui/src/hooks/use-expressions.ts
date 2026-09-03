/**
 * 表达方式的数据加载 hook 与人工复核写操作。
 *
 * 对应后端 `/api/expressions` 只读接口：按会话与复核状态过滤，四种排序口径，
 * offset 分页。词表由回合收尾处的后台学习增补（source 为「本机学习」），
 * 复核状态 ``checked`` 由 `setExpressionChecked` 与 `setExpressionsChecked`
 * 写入：确认（1）永不自动淘汰，驳回（-1）退出候选池，未复核（0）照常可用——
 * 复核不是使用的前置条件，它的职责是剔除与保护。
 */
import { useEffect, useState } from 'react'

import { apiFetch, apiMutate, UnauthorizedError } from '@/lib/api'
import { inChunks, type ListOrder } from '@/lib/list-ops'
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
  /** 排序口径；`use_*` 两档按 use_count 排。 */
  order: ListOrder
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

/** 候选数跌破下限的会话；批量删除后由后端回报。 */
export interface LowPool {
  streamId: number
  /** 删除后该会话剩余的候选条数。 */
  candidates: number
}

/** 批量删除的结果。 */
export interface BatchDeleteResult {
  deleted: number
  requested: number
  /** 删除后候选池跌破下限的会话；为空表示没有会话受损。 */
  lowPools: LowPool[]
}

/**
 * 批量删除表达方式，超过单次上限时自动分批。
 *
 * 语义与逐条删除一致，只是省去往返。请求里不存在的 ID 静默跳过，因此
 * `deleted` 可能小于 `requested`——并发删除下这属正常，不作为错误。
 *
 * 返回的 `lowPools` 是删除的真实后果：候选数低于下限的会话，其表达注入会
 * 直接停摆。调用方必须把它显示出来，不能吞掉。多批时同一会话按**最后一次**
 * 回报为准：候选数只减不增，末批的数字才是删完后的终态。
 *
 * @param ids 待删除的行 ID 列表；不限长度，内部按批量上限分批发出。
 * @returns 实际删除数、请求数与受损会话列表。
 * @throws UnauthorizedError 会话失效时抛出；其余错误原样传播，由调用方展示。
 */
export async function deleteExpressions(ids: number[]): Promise<BatchDeleteResult> {
  const pools = new Map<number, LowPool>()
  const results = await inChunks(ids, (chunk) =>
    apiMutate<BatchDeleteResult>('/api/expressions/batch-delete', 'POST', { ids: chunk }),
  )
  results.forEach((result) => {
    result.lowPools.forEach((pool) => pools.set(pool.streamId, pool))
  })
  return {
    deleted: results.reduce((sum, result) => sum + result.deleted, 0),
    requested: ids.length,
    lowPools: [...pools.values()],
  }
}

/** 批量复核的结果：实际写入条数与请求条数。 */
export interface BatchCheckedResult {
  updated: number
  requested: number
}

/**
 * 批量写复核状态，超过单次上限时自动分批。
 *
 * 批量驳回是清理噪声的主力路径：驳回不删行，因此既不会让候选池被删空，也不会
 * 像删除那样让同样的说法日后被重新学回来。请求里不存在的 ID 静默跳过。
 *
 * @param ids 待写入的行 ID 列表；不限长度，内部按批量上限分批发出。
 * @param checked 目标复核状态。
 * @returns 实际写入条数与请求条数。
 * @throws UnauthorizedError 会话失效时抛出；其余错误原样传播，由调用方展示。
 */
export async function setExpressionsChecked(
  ids: number[],
  checked: ExpressionChecked,
): Promise<BatchCheckedResult> {
  const results = await inChunks(ids, (chunk) =>
    apiMutate<{ updated: number }>('/api/expressions/batch-checked', 'POST', {
      ids: chunk,
      checked,
    }),
  )
  return {
    updated: results.reduce((sum, item) => sum + item.updated, 0),
    requested: ids.length,
  }
}
