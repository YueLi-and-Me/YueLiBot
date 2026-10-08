/**
 * 检索调优中心的数据 hook。
 *
 * 对应后端 `/api/memory/tuning*` 一组接口：参数白名单与 profile 列表、
 * 保存 / 应用 / 回滚 / 导出、按参数重放召回留痕跑一次弱监督评估。评估是
 * 同步请求，数据量随留痕回合数线性增长，页面上以加载态呈现。
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

/** 一个已保存的 profile，字段名照搬后端 `tuning.list_profiles` 的 snake_case。 */
export interface TuningProfile {
  name: string
  params: Record<string, number>
  created_at: number | null
  last_applied_at: number | null
}

/** 调优中心的总览响应。 */
export interface TuningOverview {
  whitelist: Record<string, TuningParamSpec>
  active: { profile: string; overrides: Record<string, number> }
  profiles: TuningProfile[]
}

/**
 * 一次评估的汇总报告，与后端 `Round2Report.traced_as_dict()` 逐字段对应。
 *
 * 评估重放的是生产召回留痕：检索词与正例都取自当轮的真实记录。
 * `sample_count`、`ndcg_mean`、`displaced_positive_total` 与 `per_turn` 只含
 * 可评分回合（她回了话、且当时有记忆进了提示词）；召回条数统计全部重放回合。
 *
 * 后端这一组接口原样返回 snake_case，没有转换层。曾按 camelCase 声明本类型，
 * 类型检查无从发现，运行时 `ndcgMean` 为 undefined，点评估后页面渲染崩溃。
 */
export interface TuningEvalReport {
  profile: string | null
  params: Record<string, number>
  k: number
  /** 可评分回合数，吻合度与漏挑只在这些回合上统计。 */
  sample_count: number
  /** 重放的留痕回合总数，含没有记忆进提示词的回合。 */
  replayed_count: number
  /**
   * 重放出的候选记忆与当时留痕对不上的回合数。通常源于记忆强度在留痕之后发生
   * 变化；该数越大，吻合度绝对值的可信度越低。
   */
  reconcile_mismatched: number
  ndcg_mean: number
  recall_count_median: number
  recall_count_mean: number
  /** 正例落在前 k 之外的总条数：「该进的有没有被挤掉」的直接读数。 */
  displaced_positive_total: number
  per_turn: Array<{
    turn_id: number
    stream_kind: string
    positive_count: number
    recall_count: number
    ndcg: number
    displaced_positive: number
  }>
  /** ISO 8601 时刻（UTC）。 */
  generated_at: string
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
    return apiFetch<{ profile: string; params: Record<string, number>; exported_at: string }>(
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
