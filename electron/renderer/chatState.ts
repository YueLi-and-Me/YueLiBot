/**
 * 定义聊天回合结束后的角色表情状态转换。
 *
 * 该纯函数由聊天 UI 调用，输入只来自主进程同步的睡眠状态，不读取浏览器时钟
 * 或 DOM，便于单元测试和回合状态恢复。
 */
import type { Emotion } from './character/types.ts'

/**
 * 根据主进程睡眠状态选择一轮对话结束后的角色表情。
 *
 * @param asleep 当前是否处于睡眠状态。
 * @returns {Emotion} 睡眠时返回 ``sleepy``，否则返回 ``normal``。
 * @sideEffects 不修改外部状态，仅返回状态映射结果。
 */
export function settledEmotion(asleep: boolean): Emotion {
  return asleep ? 'sleepy' : 'normal'
}
