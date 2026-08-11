/**
 * 提供日记页面使用的业务日期标签和记忆事实分组转换。
 *
 * 日期计算显式接收主进程注入的当前时间，避免渲染层时区或系统时钟差异改变
 * “今天/昨天/前天”的展示；数据结构与 diary.ts 的视图消费约定保持一致。
 */
const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

export interface RememberedFact {
  content: string
  frozen: boolean
}

/**
 * 将业务时间戳转换为日记页面使用的相对日期标签。
 *
 * @param ts 日记条目的 Unix 时间戳，单位为毫秒。
 * @param now 主进程注入的当前业务时间戳，单位为毫秒；不读取浏览器系统时钟。
 * @returns {string} 当天、前一天、前两天返回中文相对标签，其余日期返回星期标签。
 * @sideEffects 不修改输入或全局日期状态。
 */
export function diaryDayLabel(ts: number, now: number): string {
  const date = new Date(ts)
  const startOf = (value: Date): number => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime()
  const diffDays = Math.round((startOf(new Date(now)) - startOf(date)) / 86_400_000)

  if (diffDays === 0) return '今天'
  if (diffDays === 1) return '昨天'
  if (diffDays === 2) return '前天'
  return `更早的${WEEKDAYS[date.getDay()]}`
}

/**
 * 按 frozen 标志拆分记忆事实展示分组。
 *
 * @param facts 记忆事实列表；每项包含展示正文和冻结标志。
 * @returns {{remembered: string[], fading: string[]}} 未冻结正文和冻结正文的分组结果，保持输入顺序。
 * @sideEffects 不修改 facts，仅创建两个新的字符串数组。
 */
export function splitRememberedFacts(facts: RememberedFact[]): { remembered: string[]; fading: string[] } {
  const remembered: string[] = []
  const fading: string[] = []
  for (const fact of facts) {
    if (fact.frozen) fading.push(fact.content)
    else remembered.push(fact.content)
  }
  return { remembered, fading }
}
