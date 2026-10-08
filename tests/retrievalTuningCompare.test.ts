/** 检索评估「现行 vs 草稿」对照计算测试。 */
import { describe, expect, it } from 'vitest'

import type { TuningEvalReport } from '../webui/src/hooks/use-retrieval-tuning.ts'
import { compareReports, NOISE_BAND, sameParams } from '../webui/src/features/memory/tuning-compare.ts'

/** 按 [回合, 吻合度, 漏挑条数] 构造一份报告，其余字段取固定值。 */
function report(rows: Array<[number, number, number]>): TuningEvalReport {
  return {
    profile: null,
    params: {},
    k: 6,
    sample_count: rows.length,
    replayed_count: rows.length,
    reconcile_mismatched: 0,
    ndcg_mean: 0,
    recall_count_median: 6,
    recall_count_mean: 6,
    displaced_positive_total: 0,
    per_turn: rows.map(([turn, ndcg, missed]) => ({
      turn_id: turn,
      stream_kind: 'group',
      positive_count: 6,
      recall_count: 6,
      ndcg,
      displaced_positive: missed,
    })),
    generated_at: '2026-10-08T08:00:00+00:00',
  }
}

describe('检索评估对照', () => {
  it('只比较两份报告共有的回合，并按变化幅度排列', () => {
    const baseline = report([[1, 0.5, 2], [2, 0.4, 3], [3, 0.3, 1]])
    const draft = report([[2, 0.6, 1], [3, 0.2, 2], [4, 0.9, 0]])
    const result = compareReports(baseline, draft)
    expect(result).not.toBeNull()
    expect(result!.pairedTurns).toBe(2)
    expect(result!.baselineMean).toBeCloseTo(0.35)
    expect(result!.draftMean).toBeCloseTo(0.4)
    expect(result!.closerTurns).toBe(1)
    expect(result!.fartherTurns).toBe(1)
    expect(result!.unchangedTurns).toBe(0)
    expect(result!.baselineMissed).toBe(4)
    expect(result!.draftMissed).toBe(3)
    expect(result!.changed.map((row) => row.turnId)).toEqual([2, 3])
    expect(result!.verdict).toBe('closer')
  })

  it('平均变化落在波动范围内时判为差不多', () => {
    const baseline = report([[1, 0.5, 0], [2, 0.5, 0]])
    const draft = report([[1, 0.5 + NOISE_BAND / 2, 0], [2, 0.5, 0]])
    const result = compareReports(baseline, draft)
    expect(result!.verdict).toBe('similar')
    expect(result!.unchangedTurns).toBe(1)
  })

  it('平均变化低于负的波动范围时判为更远', () => {
    const result = compareReports(report([[1, 0.6, 0]]), report([[1, 0.3, 2]]))
    expect(result!.verdict).toBe('farther')
  })

  it('没有共同回合时返回 null，由页面明确提示无法对比', () => {
    expect(compareReports(report([[1, 0.5, 0]]), report([[2, 0.5, 0]]))).toBeNull()
  })

  it('草稿与现行覆盖完全一致时视为没有改动', () => {
    expect(sameParams({}, {})).toBe(true)
    expect(sameParams({ bm25_weight: 1.2 }, { bm25_weight: 1.2 })).toBe(true)
    expect(sameParams({ bm25_weight: 0.7 }, { bm25_weight: 1.2 })).toBe(false)
    expect(sameParams({ bm25_weight: 0.7 }, {})).toBe(false)
  })
})
