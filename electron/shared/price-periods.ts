/**
 * 分时段单价的时刻解析与校验，Electron 配置读取与 WebUI 模型表单共用。
 *
 * 口径与 Python 侧 `src/core/config/schema.py` 的 `PricePeriodConfig` 和
 * `ModelDefinitionConfig._validate_price_periods` 一致：时刻为两位小时、两位分钟的
 * `HH:MM`；区间含开始、不含结束；结束早于开始表示跨午夜；开始等于结束与时段重叠
 * 都是错误。报错文本带模型名，措辞与 Python 加载期报错相同，用户在哪一侧看到的
 * 都是同一句话。
 *
 * 只依赖类型定义，不引用 Node 或 DOM 接口，因此两侧都能直接导入。
 */
import type { PricePeriodConfig } from './ipc.ts'

/** 24 小时制两位小时与两位分钟，不接受 24:00 与单位数小时。 */
const CLOCK_PATTERN = /^([01]\d|2[0-3]):([0-5]\d)$/
/** 一天的分钟数；时刻换算成 [0, 1440) 内的分钟序号比较。 */
const MINUTES_PER_DAY = 24 * 60

/**
 * 把 `HH:MM` 换算成当天的分钟序号。
 *
 * @param text 时刻文本。
 * @returns 0 到 1439 的分钟序号；格式不合法时为 null。
 */
export function parseClockMinutes(text: string): number | null {
  const match = CLOCK_PATTERN.exec(text)
  if (!match) return null
  return Number(match[1]) * 60 + Number(match[2])
}

/**
 * 把时段展开成不跨午夜的半开分钟区间。
 *
 * @param start 开始分钟序号。
 * @param end 结束分钟序号。
 * @returns 一到两个 `[开始, 结束)`；跨午夜时拆成到 1440 与从 0 开始的两段。
 */
function segments(start: number, end: number): Array<[number, number]> {
  if (start < end) return [[start, end]]
  return end ? [[start, MINUTES_PER_DAY], [0, end]] : [[start, MINUTES_PER_DAY]]
}

/**
 * 找出一个模型的分时段单价里的第一处错误。
 *
 * @param modelName 模型名，写进报错。
 * @param periods 待校验的时段，只读开始与结束。
 * @returns 报错文本；没有错误时为 null。
 */
export function pricePeriodProblem(
  modelName: string,
  periods: ReadonlyArray<Pick<PricePeriodConfig, 'start' | 'end'>>,
): string | null {
  const spans: Array<{ index: number; start: number; end: number }> = []
  for (const [index, period] of periods.entries()) {
    const label = `模型 ${modelName} 的 price_periods 第 ${index + 1} 段`
    const start = parseClockMinutes(period.start)
    if (start === null) return `${label}的 start '${period.start}' 不是合法的 HH:MM 时刻`
    const end = parseClockMinutes(period.end)
    if (end === null) return `${label}的 end '${period.end}' 不是合法的 HH:MM 时刻`
    if (start === end) return `${label}的开始与结束相同（${period.start}），时段长度不能为 0`
    for (const [segmentStart, segmentEnd] of segments(start, end)) {
      spans.push({ index, start: segmentStart, end: segmentEnd })
    }
  }
  for (const [position, span] of spans.entries()) {
    for (const other of spans.slice(position + 1)) {
      if (other.index !== span.index && span.start < other.end && other.start < span.end) {
        const first = periods[span.index]
        const second = periods[other.index]
        if (!first || !second) continue
        return `模型 ${modelName} 的 price_periods 第 ${span.index + 1} 段`
          + `（${first.start}～${first.end}）与第 ${other.index + 1} 段`
          + `（${second.start}～${second.end}）时间重叠`
      }
    }
  }
  return null
}
