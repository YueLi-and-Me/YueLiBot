const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

export interface RememberedFact {
  content: string
  frozen: boolean
}

/** 日记日期标签只接收主进程给出的业务时间，渲染层不自行读取系统时钟。 */
export function diaryDayLabel(ts: number, now: number): string {
  const date = new Date(ts)
  const startOf = (value: Date): number => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime()
  const diffDays = Math.round((startOf(new Date(now)) - startOf(date)) / 86_400_000)

  if (diffDays === 0) return '今天'
  if (diffDays === 1) return '昨天'
  if (diffDays === 2) return '前天'
  return `更早的${WEEKDAYS[date.getDay()]}`
}

/** 把仍清晰与已经淡下去的记忆分开，冻结不是删除。 */
export function splitRememberedFacts(facts: RememberedFact[]): { remembered: string[]; fading: string[] } {
  const remembered: string[] = []
  const fading: string[] = []
  for (const fact of facts) {
    if (fact.frozen) fading.push(fact.content)
    else remembered.push(fact.content)
  }
  return { remembered, fading }
}
