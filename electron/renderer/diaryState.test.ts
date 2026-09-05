/**
 * 验证日记日期标签和记忆事实拆分逻辑的边界行为。
 */
import { describe, expect, it } from 'vitest'
import { diaryDayLabel, splitRememberedFacts } from './diaryState.ts'

describe('日记业务时间', () => {
  it('使用主进程注入时刻标记今天、昨天和前天，不读取渲染层真实时钟', () => {
    const injectedNow = new Date(2051, 6, 15, 14, 0).getTime()

    expect(diaryDayLabel(new Date(2051, 6, 15, 8, 0).getTime(), injectedNow)).toBe('今天')
    expect(diaryDayLabel(new Date(2051, 6, 14, 23, 0).getTime(), injectedNow)).toBe('昨天')
    expect(diaryDayLabel(new Date(2051, 6, 13, 23, 0).getTime(), injectedNow)).toBe('前天')
  })

  it('更早日期只保留相对叙事标签，不把月日数字带进日记', () => {
    const injectedNow = new Date(2051, 6, 15, 14, 0).getTime()
    const label = diaryDayLabel(new Date(2051, 6, 10, 8, 0).getTime(), injectedNow)

    expect(label).toMatch(/^更早的周/)
    expect(label).not.toMatch(/\d/)
  })
})

describe('日记内心区块', () => {
  it('★ 已冻结记忆进入“有点想不起来了”分组，而不是从界面消失', () => {
    const groups = splitRememberedFacts([
      { content: '你不喜欢香菜', frozen: false },
      { content: '你那天说自己很累', frozen: true },
    ])

    expect(groups.remembered).toEqual(['你不喜欢香菜'])
    expect(groups.fading).toEqual(['你那天说自己很累'])
  })
})
