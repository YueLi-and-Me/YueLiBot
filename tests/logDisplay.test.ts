/** 观察面板中文显示映射与结构化详情测试。 */
import { describe, expect, it } from 'vitest'

import {
  displayValue,
  formatMessages,
  traceDetailItems,
  traceKindLabel,
  traceKindQueryValue,
  traceSourceLabel,
} from '../webui/src/lib/format.ts'

describe('观察面板中文显示', () => {
  it('用中文展示事件、枚举和模型消息角色', () => {
    expect(traceKindLabel('llm_request')).toBe('请求模型')
    expect(traceKindQueryValue('模型输出完成')).toBe('llm_final')
    expect(displayValue(['direct_question', 'topic_continuation'])).toBe('明确提问、延续当前话题')
    expect(formatMessages([{ role: 'assistant', content: '你好' }])).toBe('【机器人】\n你好')
  })

  it('把嵌套行动事件拆成独立中文信息项', () => {
    const items = traceDetailItems({
      seq: 8,
      at: 123,
      kind: 'action_decision',
      turnId: 3,
      eventStatus: 'committed',
      gate: {
        disposition: 'drop',
        reasonCodes: ['attention_filtered'],
      },
      decision: {
        action: 'reply',
        reasonCodes: ['direct_question'],
      },
    })

    expect(items).toEqual([
      { rawKey: 'eventStatus', label: '事件状态', value: '已决定执行' },
      { rawKey: 'gate.disposition', label: '门控信息 · 门控结果', value: '拦截' },
      { rawKey: 'gate.reasonCodes', label: '门控信息 · 决策理由', value: '未进入注意范围' },
      { rawKey: 'decision.action', label: '最终决策 · 动作', value: '回复' },
      { rawKey: 'decision.reasonCodes', label: '最终决策 · 决策理由', value: '明确提问' },
    ])
  })

  it('在事件账本中直接展示后端来源标签', () => {
    expect(traceSourceLabel({
      seq: 9,
      at: 123,
      kind: 'user_input',
      streamId: 3,
      sourceLabel: '群聊·629201002',
    })).toBe('群聊·629201002')
  })
})
