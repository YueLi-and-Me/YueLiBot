import { describe, expect, it } from 'vitest'

import { mentionsScreen } from '../src/main/screenIntent.ts'

describe('屏幕意图识别', () => {
  it('点名屏幕上的东西时触发', () => {
    for (const line of [
      '看看我屏幕',
      '能看到我的屏幕吗',
      '小璃看看我桌面',
      '现在呢？屏幕是什么？给我描述一下',
      '给我描述一下屏幕界面',
      '这个画面你看得懂吗',
    ]) {
      expect(mentionsScreen(line), line).toBe(true)
    }
  })

  it('问「我在干嘛」时触发——答它必须看画面', () => {
    for (const line of ['看看我在干嘛', '我在干什么', '你猜我在忙啥', '干嘛呢']) {
      expect(mentionsScreen(line), line).toBe(true)
    }
  })

  it('招呼她往这边看时触发', () => {
    for (const line of ['看看我', '瞅一眼我这边', '瞧瞧这里']) {
      expect(mentionsScreen(line), line).toBe(true)
    }
  })

  it('普通闲聊不触发——每句都截既花钱又把画面送上云', () => {
    for (const line of [
      '小璃在吗',
      '晚上吃什么好',
      '今天好累啊',
      '你喜欢我吗',
      '帮我想个变量名',
      '我昨天梦到你了',
      '',
      '   ',
    ]) {
      expect(mentionsScreen(line), line).toBe(false)
    }
  })

  it('单个「看看」不算，太容易误判', () => {
    // 「看看这段代码」是让她读粘贴的文本，不是让她看屏幕
    expect(mentionsScreen('看看这段代码对不对')).toBe(false)
  })
})
