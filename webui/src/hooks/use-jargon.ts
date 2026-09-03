/**
 * 黑话词表的数据加载 hook 与人工复核写操作。
 *
 * 对应后端 `/api/jargon` 只读接口（按状态、会话、是否全局与关键词过滤，四种
 * 排序口径，offset 分页），以及 `/api/jargon/{id}/status`、`/api/jargon/{id}`
 * 两个逐条写接口和 `batch-status`、`batch-delete` 两个批量写接口。查询参数对象
 * 变化即重新读取；关键词防抖由页面侧用提交式输入框承担，这里不做时间防抖。
 */
import { useEffect, useState } from 'react'

import { apiFetch, apiMutate, UnauthorizedError } from '@/lib/api'
import { inChunks, type ListOrder } from '@/lib/list-ops'
import { useAuth } from './use-auth'

/**
 * 词条状态：`confirmed` 参与提示词注入，`pending` 是尚未判定的候选（只存不用），
 * `rejected` 是人工驳回的条目（退出注入且锁定推断，不会被自动判回来）。
 */
export type JargonStatus = 'confirmed' | 'pending' | 'rejected'

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
  /** 学习期累计出现次数（每批语料至多 +1），阶梯阈值判据。 */
  sightings: number
  /** 上次推断时的 sightings 值；0 表示尚未判定过。 */
  inferredAtSightings: number
}

/** 一组过滤、排序与分页条件。 */
export interface JargonQuery {
  status: JargonStatus
  streamId: number | null
  globalOnly: boolean
  keyword: string
  /** 排序口径；`use_*` 两档按查表命中数 hits 排。 */
  order: ListOrder
  limit: number
  offset: number
  /** 复核或删除写入后递增以触发重新拉取。 */
  refreshKey?: number
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
 * @param query 过滤、排序与分页条件。
 * @returns 词条列表、总数、加载态与错误文本。
 */
export function useJargon(query: JargonQuery): JargonState {
  const { handleUnauthorized } = useAuth()
  const [entries, setEntries] = useState<JargonEntry[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const { status, streamId, globalOnly, keyword, order, limit, offset, refreshKey } = query

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const params = new URLSearchParams({
      status,
      order,
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
  }, [status, streamId, globalOnly, keyword, order, limit, offset, refreshKey, handleUnauthorized])

  return { entries, total, loading, error }
}

/**
 * 写一条黑话词条的复核状态。
 *
 * 三个目标状态的口径（后端会连带改写推断阶梯，见 `_set_jargon_status`）：
 * 确认与驳回都会锁定推断，人的结论不再被自动判定覆盖；改回待定则解锁，把这条
 * 词交回给自动判定重新走一遍。
 *
 * @param id 词条行 ID。
 * @param status 目标状态。
 * @returns 后端回写的实际状态。
 * @throws UnauthorizedError 会话失效时抛出；其余错误原样传播，由调用方展示。
 */
export async function setJargonStatus(
  id: number,
  status: JargonStatus,
): Promise<{ id: number; status: JargonStatus }> {
  return apiMutate(`/api/jargon/${id}/status`, 'PUT', { status })
}

/**
 * 删除一条黑话词条。
 *
 * 与驳回的分工：驳回保留行并锁住推断，是「判过了」的记号，学习器再学到同一个词
 * 只会累加证据；删除不可逆，同一个词日后会作为全新候选重新入库、从待定重走一遍
 * 判定。清理误抽取的噪声用删除，压制一个真实存在但不该注入的词用驳回。
 *
 * @param id 词条行 ID。
 * @returns 被删除的 ID。
 * @throws UnauthorizedError 会话失效时抛出；其余错误原样传播，由调用方展示。
 */
export async function deleteJargon(id: number): Promise<{ id: number }> {
  return apiMutate(`/api/jargon/${id}`, 'DELETE')
}

/** 批量写操作的结果：实际生效条数与请求条数。 */
export interface JargonBatchResult {
  affected: number
  requested: number
}

/**
 * 批量写复核状态，超过单次上限时自动分批。
 *
 * 语义与逐条一致，只是省去往返。请求里不存在的 ID 静默跳过，因此 `affected`
 * 可能小于 `requested`。
 *
 * @param ids 待写入的行 ID 列表；不限长度，内部按上限分批发出。
 * @param status 目标状态。
 * @returns 实际写入条数与请求条数。
 * @throws UnauthorizedError 会话失效时抛出；其余错误原样传播，由调用方展示。
 */
export async function setJargonStatuses(
  ids: number[],
  status: JargonStatus,
): Promise<JargonBatchResult> {
  const results = await inChunks(ids, (chunk) =>
    apiMutate<{ updated: number }>('/api/jargon/batch-status', 'POST', { ids: chunk, status }),
  )
  return {
    affected: results.reduce((sum, item) => sum + item.updated, 0),
    requested: ids.length,
  }
}

/**
 * 批量删除黑话词条，超过单次上限时自动分批。
 *
 * @param ids 待删除的行 ID 列表；不存在的 ID 静默跳过。
 * @returns 实际删除条数与请求条数。
 * @throws UnauthorizedError 会话失效时抛出；其余错误原样传播，由调用方展示。
 */
export async function deleteJargonEntries(ids: number[]): Promise<JargonBatchResult> {
  const results = await inChunks(ids, (chunk) =>
    apiMutate<{ deleted: number }>('/api/jargon/batch-delete', 'POST', { ids: chunk }),
  )
  return {
    affected: results.reduce((sum, item) => sum + item.deleted, 0),
    requested: ids.length,
  }
}
