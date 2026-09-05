/**
 * 验证聊天回合结束时表情状态按睡眠状态回落的纯函数行为。
 */
import { describe, expect, it } from 'vitest'
import { settledEmotion } from './chatState.ts'

describe('对话表情回落', () => {
  it('★ 睡眠态下说完话会回落到 sleepy，而不是 normal', () => {
    expect(settledEmotion(true)).toBe('sleepy')
    expect(settledEmotion(false)).toBe('normal')
  })
})
