/**
 * 检索评估的对照计算：把「现行参数」与「草稿」两份评估报告按回合配对，
 * 算出吻合度变化、更接近 / 更远 / 没变的回合数，以及一句话结论。
 *
 * 只做纯计算，不发请求、不碰界面；被 `RetrievalTuningPage` 调用，
 * 单测在 `tests/retrievalTuningCompare.test.ts`。
 */
import type { TuningEvalReport } from '../../hooks/use-retrieval-tuning'

/**
 * 平均吻合度（nDCG）变化不到该值时判为「差不多」，取值 0 到 1。
 *
 * 依据：09 月第二轮评估里，给重放加上向量融合使整体 nDCG 变化 +0.0145，
 * 逐回合升降基本对称（升 35、降 39），当时判定为扰动而非改善。
 */
export const NOISE_BAND = 0.02

/** 对照结论：草稿挑出的记忆和她实际用过的更接近、更远，还是差不多。 */
export type CompareVerdict = 'closer' | 'farther' | 'similar'

/** 单个回合在两组参数下的吻合度。 */
export interface TurnDelta {
  turnId: number
  streamKind: string
  baseline: number
  draft: number
  /** 草稿减现行；正数表示草稿更接近她当时实际用的记忆。 */
  delta: number
}

/** 两份报告的对照结果；所有数字只统计两份报告共有的回合。 */
export interface ReportComparison {
  /** 共有回合数。两次评估之间样本窗口可能滚动，只比较共同部分。 */
  pairedTurns: number
  baselineMean: number
  draftMean: number
  /** 草稿平均吻合度减现行平均吻合度。 */
  delta: number
  closerTurns: number
  fartherTurns: number
  unchangedTurns: number
  /** 共有回合里，现行参数下漏挑的记忆总条数。 */
  baselineMissed: number
  /** 共有回合里，草稿参数下漏挑的记忆总条数。 */
  draftMissed: number
  verdict: CompareVerdict
  /** 吻合度有变化的回合，按变化幅度从大到小排列。 */
  changed: TurnDelta[]
}

/**
 * 按回合配对两份评估报告并给出结论。
 *
 * @param baseline 用现行参数跑出的报告。
 * @param draft 用草稿参数跑出的报告。
 * @returns 对照结果；两份报告没有共同回合时返回 `null`，由调用方明确提示无法对比。
 */
export function compareReports(
  baseline: TuningEvalReport,
  draft: TuningEvalReport,
): ReportComparison | null {
  const baselineByTurn = new Map(baseline.per_turn.map((row) => [row.turn_id, row]))
  const deltas: TurnDelta[] = []
  let baselineMissed = 0
  let draftMissed = 0
  for (const row of draft.per_turn) {
    const before = baselineByTurn.get(row.turn_id)
    if (!before) continue
    deltas.push({
      turnId: row.turn_id,
      streamKind: row.stream_kind,
      baseline: before.ndcg,
      draft: row.ndcg,
      delta: row.ndcg - before.ndcg,
    })
    baselineMissed += before.displaced_positive
    draftMissed += row.displaced_positive
  }
  if (deltas.length === 0) return null

  const baselineMean = deltas.reduce((sum, row) => sum + row.baseline, 0) / deltas.length
  const draftMean = deltas.reduce((sum, row) => sum + row.draft, 0) / deltas.length
  const delta = draftMean - baselineMean
  // 后端把单回合 nDCG 四舍五入到 4 位小数，两侧同口径，直接比较即可判定「没变」。
  const changed = deltas
    .filter((row) => row.delta !== 0)
    .sort((left, right) => Math.abs(right.delta) - Math.abs(left.delta))
  const closerTurns = changed.filter((row) => row.delta > 0).length

  let verdict: CompareVerdict = 'similar'
  if (delta >= NOISE_BAND) verdict = 'closer'
  else if (delta <= -NOISE_BAND) verdict = 'farther'

  return {
    pairedTurns: deltas.length,
    baselineMean,
    draftMean,
    delta,
    closerTurns,
    fartherTurns: changed.length - closerTurns,
    unchangedTurns: deltas.length - changed.length,
    baselineMissed,
    draftMissed,
    verdict,
    changed,
  }
}

/**
 * 判断草稿参数集是否与现行覆盖完全一致（一致时对比没有意义）。
 *
 * @param draftParams 草稿折叠后的完整参数集。
 * @param activeOverrides 当前生效的覆盖表。
 * @returns 两者键集合与取值完全相同时为 `true`。
 */
export function sameParams(
  draftParams: Record<string, number>,
  activeOverrides: Record<string, number>,
): boolean {
  const draftKeys = Object.keys(draftParams)
  if (draftKeys.length !== Object.keys(activeOverrides).length) return false
  return draftKeys.every((key) => activeOverrides[key] === draftParams[key])
}
