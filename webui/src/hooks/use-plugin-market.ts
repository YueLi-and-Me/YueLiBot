/**
 * 市场目录和写操作状态，供插件市场与工具页共享。
 * 所有接口复用同源鉴权封装，写完只刷新快照，不自动重启。
 */
import { useCallback, useEffect, useState } from 'react'
import { apiFetch, apiMutate, UnauthorizedError } from '@/lib/api'
import { useAuth } from '@/hooks/use-auth'
import type { MarketAction, MarketResult, MarketSnapshot } from '@/features/plugin-market/types'

/**
 * 加载目录并执行市场操作，结果保留依赖错误原文供页面展示与复制。
 * @returns 目录快照、错误和操作状态，以及刷新、写入方法。
 * @remarks 挂载时读取目录；401 交给认证上下文，其余错误展示中文原文。
 */
export function usePluginMarket() {
  const { handleUnauthorized } = useAuth()
  const [snapshot, setSnapshot] = useState<MarketSnapshot | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<MarketResult | null>(null)
  const [resultId, setResultId] = useState('')
  const load = useCallback(async (refresh = false) => {
    setError('')
    try { setSnapshot(await apiFetch<MarketSnapshot>(`/plugin-market/index?refresh=${refresh ? 1 : 0}`)) }
    catch (cause) {
      if (cause instanceof UnauthorizedError) handleUnauthorized(cause)
      else setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [handleUnauthorized])
  useEffect(() => { void load() }, [load])
  const act = useCallback(async (action: MarketAction, payload: { id: string; version?: string; source?: string }) => {
    setBusy(true); setError(''); setResult(null)
    try {
      const response = await apiMutate<MarketResult>(`/plugin-market/${action}`, 'POST', payload)
      setResult(response); setResultId(payload.id)
      await load()
      return response
    } catch (cause) {
      if (cause instanceof UnauthorizedError) handleUnauthorized(cause)
      else setError(cause instanceof Error ? cause.message : String(cause))
      return null
    } finally { setBusy(false) }
  }, [load, handleUnauthorized])
  return { snapshot, error, busy, result, resultId, load, act }
}
