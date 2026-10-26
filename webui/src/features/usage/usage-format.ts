/**
 * 用量统计页的数据形状与纯函数：时间范围、千分位、费用小数位、耗时、分组行。
 *
 * 数字格式规则与后端报告文件、控制台汇总（`src/core/services/maintenance/usage_report.py`）
 * 一致；时间范围的起止与 `src/core/llm_models/usage_stats.py` 的 `range_bounds` 一致，
 * 页面换算出起止后交给 `/api/usage`。不依赖 React，便于单测。
 */
import { taskLabel } from '@/lib/model-tasks'

/** 一小时与一天的毫秒数；小时桶按 UTC 整点对齐。 */
export const HOUR_MS = 3_600_000
export const DAY_MS = 24 * HOUR_MS

/** 0 号会话不属于任何会话：日程、摘要、记忆抽取等后台任务发起的调用。 */
export const BACKGROUND_STREAM_LABEL = '后台任务'

/** 时间范围名，与后端 RANGE_KEYS 一致。 */
export type RangeKey = 'hour' | 'today' | '7d' | '30d' | 'all'

/** 页面顶部的时间范围选项，顺序即展示顺序。 */
export const RANGE_OPTIONS: Array<{ value: RangeKey; label: string }> = [
  { value: 'hour', label: '本小时' },
  { value: 'today', label: '今天' },
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
  { value: 'all', label: '全部' },
]

/** 一组用量合计，对应后端 summarize_usage 的合计字段。 */
export interface UsageTotals {
  calls: number
  failedCalls: number
  interruptedCalls: number
  unreportedCalls: number
  promptTokens: number
  completionTokens: number
  totalTokens: number
  cachedTokens: number
  reasoningTokens: number
  /** 费用，单位元。 */
  cost: number
  /** 平均耗时毫秒；没有调用时为 null。 */
  avgDurationMs: number | null
  /** 耗时标准差毫秒；没有调用时为 null。 */
  durationStdMs: number | null
}

/** 按模型分组时附带的当前单价。 */
export interface ModelPriceInfo {
  priceIn: number
  priceOut: number
  cachePriceIn: number | null
  periods: number
  /** 基础单价全为 0 且没有分时段单价。 */
  unpriced: boolean
}

/** 后端返回的一个分组。 */
export interface UsageGroup extends UsageTotals {
  key: string
  /** 来源与会话分组的可读名称。 */
  label?: string
  /** 来源分组：主程序、内置插件或第三方插件。 */
  kind?: 'main' | 'builtin' | 'third_party'
  /** 模型分组的模型名与厂商。 */
  model?: string
  provider?: string
  /** 模型分组的当前单价；模型已不在配置中时为 null。 */
  price?: ModelPriceInfo | null
  /** 会话分组的会话编号；0 表示后台任务。 */
  streamId?: number
}

/** 趋势序列的一个点。 */
export interface TrendPoint {
  /** 桶起点 Unix 毫秒。 */
  start: number
  tokens: number
  cost: number
}

/** `/api/usage` 的响应。 */
export interface UsageSummary {
  range: { start: number; end: number; tzOffsetMinutes: number }
  generatedAt: number
  overview: UsageTotals & { replies: number }
  /** 回复数为 0 时三项均为 null。 */
  perReply: { cost: number | null; tokens: number | null; calls: number | null }
  unreported: Array<{ model: string; provider: string; calls: number }>
  groups: Record<GroupKind, UsageGroup[]>
  trend: { hourly: TrendPoint[]; daily: TrendPoint[] }
}

/** 分组方式。 */
export type GroupKind = 'source' | 'task' | 'model' | 'stream'

/**
 * 当前时区相对 UTC 的分钟偏移，东区为正。
 *
 * @param now 取偏移的时刻；夏令时地区偏移随时刻变化。
 * @returns 如东八区为 480。
 */
export function tzOffsetMinutes(now: Date): number {
  // getTimezoneOffset 的符号与通常写法相反：东八区返回 -480。
  return -now.getTimezoneOffset()
}

/**
 * 把时间范围名换算成半开区间。
 *
 * @param key 时间范围名。
 * @param nowMs 当前 Unix 毫秒。
 * @param offsetMinutes 东区为正的时区偏移分钟数。
 * @returns 起止 Unix 毫秒；止点是当前小时桶的结束，「近 N 天」按本地日计、含今天。
 */
export function rangeBounds(key: RangeKey, nowMs: number, offsetMinutes: number): { start: number; end: number } {
  const hourStart = nowMs - (nowMs % HOUR_MS)
  const end = hourStart + HOUR_MS
  const offset = offsetMinutes * 60_000
  const today = Math.floor((nowMs + offset) / DAY_MS) * DAY_MS - offset
  switch (key) {
    case 'hour': return { start: hourStart, end }
    case 'today': return { start: today, end }
    case '7d': return { start: today - 6 * DAY_MS, end }
    case '30d': return { start: today - 29 * DAY_MS, end }
    case 'all': return { start: 0, end }
  }
}

/**
 * token 数加千分位。
 *
 * @param value token 数。
 * @returns 如 `12,345`。
 */
export function formatTokens(value: number): string {
  return Math.round(value).toLocaleString('en-US')
}

/**
 * 把费用（元）格式化成展示文本。
 *
 * @param value 费用，单位元。
 * @returns 0 为 `¥0`；不足 0.0001 元为 `<¥0.0001`，避免四舍五入成 0 让人以为免费；
 * 不足 1 元保留四位小数；其余保留两位并加千分位。
 */
export function formatCost(value: number): string {
  if (value === 0) return '¥0'
  if (value < 0.0001) return '<¥0.0001'
  if (value < 1) return `¥${value.toFixed(4)}`
  return `¥${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/**
 * 平均耗时与波动。
 *
 * @param average 平均耗时毫秒。
 * @param deviation 标准差毫秒。
 * @returns 如 `2.31 s ±0.42`；没有调用时为 `—`。
 */
export function formatDuration(average: number | null, deviation: number | null): string {
  if (average === null || deviation === null) return '—'
  return `${(average / 1000).toFixed(2)} s ±${(deviation / 1000).toFixed(2)}`
}

/**
 * 占比文本。
 *
 * @param part 部分。
 * @param total 总量。
 * @returns 如 `12.3%`；总量为 0 时为 `—`。
 */
export function formatShare(part: number, total: number): string {
  if (!total) return '—'
  return `${((part / total) * 100).toFixed(1)}%`
}

/**
 * 单价组的展示文本。
 *
 * @param price 当前单价；模型已不在配置中时为 null。
 * @returns 形如 `¥2 / ¥8 / 缓存 ¥0.5 · 另有 2 个时段价`。
 */
export function priceLabel(price: ModelPriceInfo | null | undefined): string {
  if (!price) return '已不在当前配置中'
  const cache = price.cachePriceIn === null ? '缓存同输入' : `缓存 ¥${price.cachePriceIn}`
  const periods = price.periods ? ` · 另有 ${price.periods} 个时段价` : ''
  return `¥${price.priceIn} / ¥${price.priceOut} / ${cache}${periods}`
}

/** 分组表的一行。 */
export interface GroupRow extends UsageTotals {
  key: string
  /** 第一列的名称。 */
  name: string
  /** 名称下的小字：任务键名、厂商或来源类型。 */
  detail: string
  /** 模型分组的当前单价文本；其他分组为空串。 */
  price: string
  /** 单价需要提醒：为 0（没填或免费）或模型已不在配置中。 */
  priceWarning: boolean
  /** 费用占本范围总费用的比例文本。 */
  share: string
}

/** 来源类型的中文名。 */
const SOURCE_KIND_LABELS: Record<NonNullable<UsageGroup['kind']>, string> = {
  main: '主程序',
  builtin: '内置插件',
  third_party: '第三方插件',
}

/**
 * 把后端的分组转成分组表的行：名称、说明、单价与占比。
 *
 * @param kind 分组方式。
 * @param summary 当前时间范围的汇总。
 * @returns 与后端顺序一致（费用、token、次数从高到低）的行。
 */
export function groupRows(kind: GroupKind, summary: UsageSummary): GroupRow[] {
  const totalCost = summary.overview.cost
  return summary.groups[kind].map((group) => {
    let name = group.label ?? group.key
    let detail = ''
    let price = ''
    let priceWarning = false
    if (kind === 'source') {
      const sourceKind = group.kind ?? 'main'
      name = sourceKind === 'main' ? SOURCE_KIND_LABELS.main : group.key
      // 插件行在 id 下注明内置还是第三方；主程序行不重复写一遍「主程序」。
      detail = sourceKind === 'main' ? '' : SOURCE_KIND_LABELS[sourceKind]
    } else if (kind === 'task') {
      name = taskLabel(group.key)
      detail = group.key
    } else if (kind === 'model') {
      name = group.model ?? group.key
      detail = group.provider ?? ''
      price = priceLabel(group.price)
      priceWarning = !group.price || group.price.unpriced
    } else if (group.streamId === 0) {
      name = BACKGROUND_STREAM_LABEL
    }
    return {
      ...group,
      key: group.key,
      name,
      detail,
      price,
      priceWarning,
      share: formatShare(group.cost, totalCost),
    }
  })
}
