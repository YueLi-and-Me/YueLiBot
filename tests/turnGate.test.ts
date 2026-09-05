import { describe, expect, it } from 'vitest'

import { gateTurnEvent } from '../electron/renderer/turnGate.ts'

describe('桌面聊天轮次门控', () => {
  it('新轮开始后丢弃迟到的旧轮增量', () => {
    const started = gateTurnEvent(7, { turnId: 8, kind: 'start' })
    const late = gateTurnEvent(started.currentTurn, {
      turnId: 7,
      kind: 'parse',
      event: { type: 'text', value: '旧回复残句' },
    })

    expect(started).toEqual({ currentTurn: 8, accept: true, started: true })
    expect(late).toEqual({ currentTurn: 8, accept: false, started: false })
  })
})
