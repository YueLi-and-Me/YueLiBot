/**
 * 检索调优中心的数据 hook。
 *
 * 对应后端 `/api/memory/tuning*` 一组接口：参数白名单与 profile 列表、
 * 保存 / 应用 / 回滚 / 导出、按参数跑一次弱监督评估。评估是同步请求，
 * 数据量随回合样本数线性增长，页面上以加载态呈现。
 */
import { useCallback, useEffect, useState } from 'react'

import { apiFetch, apiMutate, UnauthorizedError } from '@/lib/api'
import { useAuth } from './use-auth'

/** 白名单里一个参数的取值域说明。 */
export interface TuningParamSpec {
  kind: 'int' | 'float'
  min: number
  max: number
  /** 现状值；`null` 表示该参数现状由配置提供。 */
  legacy: number | null
  label: string
}

/** 一个已保存的 profile。 */
export interface TuningProfile {
  name: string
  params: Record<string, number>
  createdAt: number | null
  lastAppliedAt: number | null
}

/** 调优中心的总览响应。 */
export interface TuningOverview {
  whitelist: Record<string, TuningParamSpec>
  active: { profile: string; overrides: Record<string, number> }
  profiles: TuningProfile[]
}

/** 一次评估的汇总报告，字段与后端 EvalReport 一一对应。 */
export interface TuningEvalReport {
  profile: string | null
  params: Record<string, number>
  k: number
  sampleCount: number
  ndcgMean: number
  recallCountMedian: number
  recallCountMean: number
  /** 正例落在前 k 之外的总条数：「该进的有没有被挤掉」的直接读数。 */
  displacedPositiveTotal: number
  perTurn: Array<{
    turnId: number
    streamKind: string
    positiveCount: number
    recallCount: number
    ndcg: number
    displacedPositive: number
  }>
  generatedAt: string
}

/** 使用检索调优中心状态与操作的 hook。 */
export function useRetrievalTuning() {
  const { authenticated } = useAuth()
  const [overview, setOverview] = useState<TuningOverview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const reload = useCallback(async () => {
    if (!authenticated) return
    setLoading(true)
    setError(null)
    try {
      setOverview(await apiFetch<TuningOverview>('/api/memory/tuning'))
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) setError(String(err))
    } finally {
      setLoading(false)
    }
  }, [authenticated])

  useEffect(() => {
    void reload()
  }, [reload])

  const saveProfile = useCallback(
    async (name: string, params: Record<string, number>) => {
      await apiMutate<{ name: string; params: Record<string, number> }>(
        '/api/memory/tuning/profiles',
        'POST',
        { name, params },
      )
      await reload()
    },
    [reload],
  )

  const deleteProfile = useCallback(
    async (name: string) => {
      await apiMutate(`/api/memory/tuning/profiles/${encodeURIComponent(name)}`, 'DELETE')
      await reload()
    },
    [reload],
  )

  const applyProfile = useCallback(
    async (name: string) => {
      await apiMutate('/api/memory/tuning/apply', 'POST', { name })
      await reload()
    },
    [reload],
  )

  const rollback = useCallback(async () => {
    await apiMutate('/api/memory/tuning/rollback', 'POST')
    await reload()
  }, [reload])

  const exportProfile = useCallback(async (name: string) => {
    return apiFetch<{ profile: string; params: Record<string, number>; exportedAt: string }>(
      `/api/memory/tuning/export?name=${encodeURIComponent(name)}`,
    )
  }, [])

  const evaluate = useCallback(
    async (opts: { profile?: string; params?: Record<string, number>; maxTurns?: number }) => {
      return apiMutate<TuningEvalReport>('/api/memory/tuning/evaluate', 'POST', opts)
    },
    [],
  )

  return {
    overview,
    error,
    loading,
    reload,
    saveProfile,
    deleteProfile,
    applyProfile,
    rollback,
    exportProfile,
    evaluate,
  }
}
