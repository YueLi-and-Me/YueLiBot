import type { Emotion } from './character/types.ts'

/** 一轮对话结束后的表情必须服从当前的主进程睡眠状态。 */
export function settledEmotion(asleep: boolean): Emotion {
  return asleep ? 'sleepy' : 'normal'
}
