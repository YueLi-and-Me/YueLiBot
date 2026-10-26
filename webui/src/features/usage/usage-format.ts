/**
 * 用量统计页的数据形状与纯函数：时间范围、数字与费用格式、分组行、趋势图的取数与刻度。
 *
 * 费用格式与后端报告文件、控制台汇总（`src/core/services/maintenance/usage_report.py`）
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
 * 大数缩写成 K、M、B，用于总览、分组表与纵轴刻度这类只需看量级的位置。
 *
 * @param value 非负数值。
 * @returns 不足一千原样显示；千起用 K 保留一位小数，百万起用 M、十亿起用 B 保留两位小数，
 * 都去掉末尾多余的 0，如 `950`、`12.3K`、`950K`、`1.36M`、`2M`。临界值按四舍五入后的结果
 * 换单位，999,960 显示为 `1M` 而不是 `1000K`。
 */
export function formatCompact(value: number): string {
  if (value < 1_000) return formatTokens(value)
  if (value < 999_950) return `${Number((value / 1e3).toFixed(1))}K`
  if (value < 999_995_000) return `${Number((value / 1e6).toFixed(2))}M`
  return `${Number((value / 1e9).toFixed(2))}B`
}

/**
 * 平均耗时。
 *
 * @param milliseconds 平均耗时毫秒；没有调用时为 null。
 * @returns 如 `2.31 s`；没有调用时为 `—`。
 */
export function formatSeconds(milliseconds: number | null): string {
  if (milliseconds === null) return '—'
  return `${(milliseconds / 1000).toFixed(2)} s`
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
 * 占比与趋势图的主度量：本范围产生过费用就看费用，否则看 token。
 *
 * 没填单价的部署费用恒为 0，按费用算占比只会得到一列「—」，所以退回 token。
 *
 * @param summary 当前时间范围的汇总。
 * @returns `cost` 或 `tokens`。
 */
export function primaryMetric(summary: UsageSummary): 'cost' | 'tokens' {
  return summary.overview.cost > 0 ? 'cost' : 'tokens'
}

/**
 * 本范围用到的模型中，当前单价为 0（没填或免费）的个数。
 *
 * @param summary 当前时间范围的汇总。
 * @returns 个数；已不在配置中的模型无处可填，不计入。
 */
export function unpricedModelCount(summary: UsageSummary): number {
  return summary.groups.model.filter((group) => group.price?.unpriced).length
}

/** 分组表的一行。 */
export interface GroupRow extends UsageTotals {
  key: string
  /** 第一列的名称。 */
  name: string
  /** 名称后的弱色补充：模型的厂商、插件的内置或第三方；任务、会话与主程序为空串。 */
  detail: string
  /** 悬停名称时的说明：任务键名、模型配置键；没有可补充的内容时为空串。 */
  hint: string
  /** 费用显示为「未计价」：当前单价为 0 且这段时间没有记到费用；只出现在按模型分组。 */
  unpriced: boolean
  /** 占本范围合计的比例，0～1；口径由 primaryMetric 决定，合计为 0 时为 0。 */
  share: number
}

/** 来源类型的中文名。 */
const SOURCE_KIND_LABELS: Record<NonNullable<UsageGroup['kind']>, string> = {
  main: '主程序',
  builtin: '内置插件',
  third_party: '第三方插件',
}

/**
 * 把后端的分组转成分组表的行：名称、补充说明、计价状态与占比。
 *
 * @param kind 分组方式。
 * @param summary 当前时间范围的汇总。
 * @returns 与后端顺序一致（费用、token、次数从高到低）的行。
 */
export function groupRows(kind: GroupKind, summary: UsageSummary): GroupRow[] {
  const metric = primaryMetric(summary)
  const total = metric === 'cost' ? summary.overview.cost : summary.overview.totalTokens
  return summary.groups[kind].map((group) => {
    let name = group.label ?? group.key
    let detail = ''
    let hint = ''
    let unpriced = false
    if (kind === 'source') {
      const sourceKind = group.kind ?? 'main'
      name = sourceKind === 'main' ? SOURCE_KIND_LABELS.main : group.key
      // 插件行注明内置还是第三方；主程序行不重复写一遍「主程序」。
      detail = sourceKind === 'main' ? '' : SOURCE_KIND_LABELS[sourceKind]
    } else if (kind === 'task') {
      name = taskLabel(group.key)
      hint = group.key
    } else if (kind === 'model') {
      name = group.model ?? group.key
      detail = group.provider ?? ''
      hint = group.price ? '' : '已不在当前配置中'
      // 费用在落账时按当时单价算好。当前单价为 0 而记到过费用，说明单价是后来清掉的，
      // 照常显示记到的费用。
      unpriced = Boolean(group.price?.unpriced) && group.cost === 0
    } else if (group.streamId === 0) {
      name = BACKGROUND_STREAM_LABEL
    }
    const part = metric === 'cost' ? group.cost : group.totalTokens
    return { ...group, key: group.key, name, detail, hint, unpriced, share: total ? part / total : 0 }
  })
}

/** 趋势图要画的一段序列。 */
export interface TrendView {
  /** 标题旁的窗口说明，如「今天 · 按小时」。 */
  caption: string
  /** 每根柱子代表的时长。 */
  unit: 'hour' | 'day'
  /** 按时间升序的点；「今天」补足到 24 个小时，尚未到来的小时数值为 0。 */
  points: TrendPoint[]
}

/**
 * 按所选时间范围从后端的两条趋势序列中取出要画的一段。
 *
 * 后端固定给近 48 小时与近 30 天两条序列，与范围无关；这里让趋势图跟着范围走，
 * 避免两张图各画一根柱子。「本小时」只有一个桶，画成柱状图没有意义，与「今天」
 * 同样画当天的 24 个小时。
 *
 * @param range 当前时间范围。
 * @param summary 当前时间范围的汇总。
 * @returns 要画的序列与窗口说明；「全部」的趋势只画近 30 天。
 */
export function trendView(range: RangeKey, summary: UsageSummary): TrendView {
  const { hourly, daily } = summary.trend
  if (range === 'hour' || range === 'today') {
    const dayStart = rangeBounds('today', summary.generatedAt, summary.range.tzOffsetMinutes).start
    const byStart = new Map(hourly.map((point) => [point.start, point]))
    const points = Array.from({ length: 24 }, (_, index) => {
      const start = dayStart + index * HOUR_MS
      return byStart.get(start) ?? { start, tokens: 0, cost: 0 }
    })
    return { caption: '今天 · 按小时', unit: 'hour', points }
  }
  if (range === '7d') return { caption: '近 7 天 · 按天', unit: 'day', points: daily.slice(-7) }
  return { caption: '近 30 天 · 按天', unit: 'day', points: daily }
}

/**
 * 纵轴上限：把峰值向上取到 1、2、2.5、5 乘 10 的整数次幂。
 *
 * @param peak 序列峰值。
 * @returns 不小于峰值的整齐数；峰值不大于 0 时为 1，使空图仍有刻度。
 */
export function niceCeiling(peak: number): number {
  if (peak <= 0) return 1
  const magnitude = 10 ** Math.floor(Math.log10(peak))
  for (const step of [1, 2, 2.5, 5]) {
    // 浮点乘法可能让 step * magnitude 比数学值略小，留一点余量避免跳到下一档。
    if (peak <= step * magnitude * (1 + 1e-9)) return step * magnitude
  }
  return 10 * magnitude
}
