import type { ChatStreamEvent } from '../shared/ipc.ts'

export interface TurnGateResult {
  currentTurn: number
  accept: boolean
  started: boolean
}

/** 根据轮次开始事件更新当前轮，并拒绝迟到的旧轮事件。 */
export function gateTurnEvent(
  currentTurn: number,
  event: ChatStreamEvent,
): TurnGateResult {
  if (event.kind === 'start') {
    if (event.turnId <= currentTurn) {
      return { currentTurn, accept: false, started: false }
    }
    return { currentTurn: event.turnId, accept: true, started: true }
  }
  if (event.turnId < currentTurn) {
    return { currentTurn, accept: false, started: false }
  }
  return {
    currentTurn: Math.max(currentTurn, event.turnId),
    accept: true,
    started: event.turnId > currentTurn,
  }
}
