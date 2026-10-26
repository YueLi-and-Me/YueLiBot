/**
 * 用量统计页的数据状态：按时间范围读取 `/api/usage`。
 *
 * 起止时间与时区偏移在前端按浏览器所在时区换算（见 usage-format 的 rangeBounds），
 * 后端只做汇总。401 统一上报认证上下文，其余错误转成页面状态文本。
 */
import { useCallback, useEffect, useState } from 'react'

import { apiFetch, UnauthorizedError } from '@/lib/api'
import { rangeBounds, tzOffsetMinutes, type RangeKey, type UsageSummary } from '@/features/usage/usage-format'
import { useAuth } from './use-auth'

/** useUsage 返回的页面状态与操作。 */
interface UsageState {
  /** 当前时间范围。 */
  range: RangeKey
  /** 切换时间范围并重新读取。 */
  setRange: (range: RangeKey) => void
  /** 当前范围的汇总；首次读取完成前为 null。 */
  summary: UsageSummary | null
  /** 是否正在读取。 */
  loading: boolean
  /** 错误文本；没有错误时为空串。 */
  error: string
  /** 按当前范围重新读取。 */
  reload: () => void
}

/**
 * 读取并管理用量统计页的数据。
 *
 * @returns 页面状态与操作集合。
 */
export function useUsage(): UsageState {
  const { handleUnauthorized } = useAuth()
  const [range, setRange] = useState<RangeKey>('today')
  const [summary, setSummary] = useState<UsageSummary | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    const now = new Date()
    const offset = tzOffsetMinutes(now)
    const { start, end } = rangeBounds(range, now.getTime(), offset)
    const query = new URLSearchParams({ start: String(start), end: String(end), tzOffset: String(offset) })
    setLoading(true)
    apiFetch<UsageSummary>(`/api/usage?${query.toString()}`)
      .then((payload) => {
        setSummary(payload)
        setError('')
      })
      .catch((reason: unknown) => {
        if (reason instanceof UnauthorizedError) {
          handleUnauthorized(reason)
          return
        }
        setError(`读取用量统计失败：${reason instanceof Error ? reason.message : String(reason)}`)
      })
      .finally(() => setLoading(false))
  }, [range, handleUnauthorized])

  useEffect(load, [load])

  return { range, setRange, summary, loading, error, reload: load }
}
