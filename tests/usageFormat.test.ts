/**
 * 用量统计页与模型表单的纯函数：千分位、费用小数位、0 号会话显示、时间范围换算、
 * 分时段价格校验，以及 Electron 写入器的单价写法。
 */
import { describe, expect, it } from 'vitest'

import { tomlPrice } from '../electron/main/config.ts'
import { priceFormProblem, type PriceFields } from '../webui/src/features/models/price-form.ts'
import {
  BACKGROUND_STREAM_LABEL,
  DAY_MS,
  formatCost,
  formatDuration,
  formatShare,
  formatTokens,
  groupRows,
  rangeBounds,
  type UsageGroup,
  type UsageSummary,
  type UsageTotals,
} from '../webui/src/features/usage/usage-format.ts'

const ZERO: UsageTotals = {
  calls: 0, failedCalls: 0, interruptedCalls: 0, unreportedCalls: 0,
  promptTokens: 0, completionTokens: 0, totalTokens: 0, cachedTokens: 0, reasoningTokens: 0,
  cost: 0, avgDurationMs: null, durationStdMs: null,
}

/** 构造只含给定分组的汇总。 */
function summaryWith(groups: Partial<UsageSummary['groups']>, cost: number): UsageSummary {
  return {
    range: { start: 0, end: 1, tzOffsetMinutes: 480 },
    generatedAt: 0,
    overview: { ...ZERO, cost, replies: 0 },
    perReply: { cost: null, tokens: null, calls: null },
    unreported: [],
    groups: { source: [], task: [], model: [], stream: [], ...groups },
    trend: { hourly: [], daily: [] },
  }
}

describe('数字格式', () => {
  it('token 数加千分位', () => {
    expect(formatTokens(0)).toBe('0')
    expect(formatTokens(1234)).toBe('1,234')
    expect(formatTokens(12345678)).toBe('12,345,678')
  })

  it('费用按大小取小数位，极小值不显示成 0', () => {
    expect(formatCost(0)).toBe('¥0')
    expect(formatCost(0.00004)).toBe('<¥0.0001')
    expect(formatCost(0.01234)).toBe('¥0.0123')
    expect(formatCost(0.5)).toBe('¥0.5000')
    expect(formatCost(1)).toBe('¥1.00')
    expect(formatCost(1234.567)).toBe('¥1,234.57')
  })

  it('耗时与占比在没有数据时显示为空', () => {
    expect(formatDuration(null, null)).toBe('—')
    expect(formatDuration(2310, 420)).toBe('2.31 s ±0.42')
    expect(formatShare(1, 0)).toBe('—')
    expect(formatShare(1, 8)).toBe('12.5%')
  })
})

describe('分组行', () => {
  it('0 号会话显示为后台任务，其余会话用后端给的名称', () => {
    const streams: UsageGroup[] = [
      { ...ZERO, key: '0', streamId: 0, label: '任意', cost: 0.2 },
      { ...ZERO, key: '5', streamId: 5, label: '群聊 · 月璃粉丝群', cost: 0.6 },
    ]
    const rows = groupRows('stream', summaryWith({ stream: streams }, 0.8))
    expect(rows.map((row) => row.name)).toEqual([BACKGROUND_STREAM_LABEL, '群聊 · 月璃粉丝群'])
    expect(rows.map((row) => row.share)).toEqual(['25.0%', '75.0%'])
  })

  it('任务用中文名，来源区分主程序、内置与第三方插件', () => {
    const tasks = groupRows('task', summaryWith({ task: [{ ...ZERO, key: 'planner' }] }, 0))
    expect(tasks[0]).toMatchObject({ name: '行动决策', detail: 'planner' })
    const sources = groupRows('source', summaryWith({
      source: [
        { ...ZERO, key: 'main', kind: 'main', label: '主程序' },
        { ...ZERO, key: 'forward-message', kind: 'builtin', label: 'forward-message' },
        { ...ZERO, key: 'weather', kind: 'third_party', label: 'weather' },
      ],
    }, 0))
    expect(sources.map((row) => [row.name, row.detail])).toEqual([
      ['主程序', ''], ['forward-message', '内置插件'], ['weather', '第三方插件'],
    ])
  })

  it('按模型时单价为 0 或已不在配置中的行要提醒', () => {
    const rows = groupRows('model', summaryWith({
      model: [
        { ...ZERO, key: 'a@p', model: 'a', provider: 'p', price: { priceIn: 2, priceOut: 8, cachePriceIn: 0.5, periods: 2, unpriced: false } },
        { ...ZERO, key: 'b@p', model: 'b', provider: 'p', price: { priceIn: 0, priceOut: 0, cachePriceIn: null, periods: 0, unpriced: true } },
        { ...ZERO, key: 'c@p', model: 'c', provider: 'p', price: null },
      ],
    }, 0))
    expect(rows.map((row) => row.priceWarning)).toEqual([false, true, true])
    expect(rows[0]?.price).toBe('¥2 / ¥8 / 缓存 ¥0.5 · 另有 2 个时段价')
    expect(rows[2]?.price).toBe('已不在当前配置中')
  })
})

describe('时间范围', () => {
  it('东八区的「今天」从本地零点起算，跨零点后换到新的一天', () => {
    // 2026-10-26 23:30 东八区 = 15:30 UTC；零点 = 前一天 16:00 UTC。
    const beforeMidnight = Date.UTC(2026, 9, 26, 15, 30)
    expect(rangeBounds('today', beforeMidnight, 480).start).toBe(Date.UTC(2026, 9, 25, 16, 0))
    // 2026-10-27 00:30 东八区 = 16:30 UTC。
    const afterMidnight = Date.UTC(2026, 9, 26, 16, 30)
    const today = rangeBounds('today', afterMidnight, 480)
    expect(today.start).toBe(Date.UTC(2026, 9, 26, 16, 0))
    expect(today.end).toBe(Date.UTC(2026, 9, 26, 17, 0))
    expect(rangeBounds('7d', afterMidnight, 480).start).toBe(today.start - 6 * DAY_MS)
    expect(rangeBounds('hour', afterMidnight, 480).start).toBe(Date.UTC(2026, 9, 26, 16, 0))
    expect(rangeBounds('all', afterMidnight, 480).start).toBe(0)
  })
})

describe('分时段价格表单校验', () => {
  const base: PriceFields = { name: '峰谷模型', price_in: 2, price_out: 8, cache_price_in: null, price_periods: [] }

  it('合法的跨午夜时段通过', () => {
    expect(priceFormProblem({
      ...base,
      price_periods: [
        { start: '23:00', end: '07:00', price_in: 1, price_out: 4, cache_price_in: 0 },
        { start: '07:00', end: '09:00', price_in: 2, price_out: 8, cache_price_in: null },
      ],
    })).toBeNull()
  })

  it('时刻格式错误、开始等于结束、时段重叠都报错并带模型名', () => {
    const period = { price_in: 1, price_out: 1, cache_price_in: null }
    expect(priceFormProblem({ ...base, price_periods: [{ ...period, start: '7:00', end: '08:00' }] }))
      .toBe("模型 峰谷模型 的 price_periods 第 1 段的 start '7:00' 不是合法的 HH:MM 时刻")
    expect(priceFormProblem({ ...base, price_periods: [{ ...period, start: '08:00', end: '08:00' }] }))
      .toBe('模型 峰谷模型 的 price_periods 第 1 段的开始与结束相同（08:00），时段长度不能为 0')
    expect(priceFormProblem({
      ...base,
      price_periods: [{ ...period, start: '22:00', end: '02:00' }, { ...period, start: '01:00', end: '03:00' }],
    })).toBe('模型 峰谷模型 的 price_periods 第 1 段（22:00～02:00）与第 2 段（01:00～03:00）时间重叠')
  })

  it('负数与非数字单价报错，缓存价留空合法', () => {
    expect(priceFormProblem({ ...base, price_in: -1 })).toContain('不小于 0')
    expect(priceFormProblem({ ...base, cache_price_in: Number.NaN })).toContain('缓存命中价')
    expect(priceFormProblem({ ...base, cache_price_in: 0 })).toBeNull()
  })
})

describe('Electron 写入器的单价写法', () => {
  it('统一写成定点小数，与 Python 写入器同口径', () => {
    expect(tomlPrice(0)).toBe('0.0')
    expect(tomlPrice(2)).toBe('2.0')
    expect(tomlPrice(0.5)).toBe('0.5')
    expect(tomlPrice(0.00005)).toBe('0.00005')
    expect(tomlPrice(5e-7)).toBe('0.0000005')
    expect(tomlPrice(1e21)).toBe('1000000000000000000000.0')
  })
})
