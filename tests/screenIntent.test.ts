/**
 * 屏幕意图识别规则测试。
 *
 * 本模块属于 Electron 主进程屏幕采集触发层的 Vitest 测试，验证直接提及屏幕、
 * 询问当前操作以及指示查看当前位置时会触发采集，同时排除普通闲聊和仅含“看看”的歧义表达。
 * 被测实现位于 electron/main/screenIntent.ts。
 */
import { describe, expect, it } from 'vitest'

import { mentionsScreen } from '../electron/main/screenIntent.ts'

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

  it('询问当前操作时触发屏幕意图', () => {
    for (const line of ['看看我在干嘛', '我在干什么', '你猜我在忙啥', '干嘛呢']) {
      expect(mentionsScreen(line), line).toBe(true)
    }
  })

  it('指示查看当前位置时触发屏幕意图', () => {
    for (const line of ['看看我', '瞅一眼我这边', '瞧瞧这里']) {
      expect(mentionsScreen(line), line).toBe(true)
    }
  })

  it('普通闲聊不触发，避免无关请求采集屏幕', () => {
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
    // “看看这段代码”指向文本内容，不足以证明用户要求采集屏幕。
    expect(mentionsScreen('看看这段代码对不对')).toBe(false)
  })
})
