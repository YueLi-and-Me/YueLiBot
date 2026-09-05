/**
 * 输入活动采集器的单元测试。
 *
 * 本模块属于 Electron 主进程输入监测层的 Vitest 测试，使用可控的钩子替身验证事件计数、
 * 鼠标位移累计、读取后清零以及底层钩子启动失败时的 idle 降级行为。
 * 被测实现位于 electron/main/inputActivity.ts。
 */
import { describe, expect, it } from 'vitest'

import { InputActivity } from '../electron/main/inputActivity.ts'

/**
 * 为 InputActivity 提供可编程事件和启动结果的最小钩子替身。
 */
class FakeInputHook {
  private readonly listeners = new Map<string, Set<(event?: { x: number; y: number }) => void>>()
  starts = 0
  stops = 0
  failStart = false

  /**
   * 注册指定事件的监听器。
   *
   * @param {string} event 事件名称；测试中使用 keydown、click 和 mousemove。
   * @param {(event?: { x: number; y: number }) => void} listener 接收可选坐标载荷的回调。
   * @returns {void} 不返回值。
   * @sideEffects 将监听器加入对应事件集合；重复注册同一函数不会产生重复调用。
   */
  on(event: string, listener: (event?: { x: number; y: number }) => void): void {
    const listeners = this.listeners.get(event) ?? new Set()
    listeners.add(listener)
    this.listeners.set(event, listeners)
  }

  /**
   * 移除指定事件的监听器。
   *
   * @param {string} event 事件名称。
   * @param {(event?: { x: number; y: number }) => void} listener 已注册的监听器。
   * @returns {void} 不返回值；事件或监听器不存在时保持静默。
   * @sideEffects 修改对应事件的监听器集合。
   */
  removeListener(event: string, listener: (event?: { x: number; y: number }) => void): void {
    this.listeners.get(event)?.delete(listener)
  }

  /**
   * 模拟底层输入钩子的启动，并按测试配置抛出启动错误。
   *
   * @returns {void} 启动成功时不返回值。
   * @throws {Error} failStart 为 true 时抛出模拟的底层钩子启动错误。
   * @sideEffects 将 starts 加一。
   */
  start(): void {
    this.starts++
    if (this.failStart) throw new Error('模拟钩子被安全软件拦截')
  }

  /**
   * 模拟停止底层输入钩子。
   *
   * @returns {void} 不返回值。
   * @sideEffects 将 stops 加一。
   */
  stop(): void {
    this.stops++
  }

  /**
   * 向指定事件同步派发测试载荷。
   *
   * @param {string} event 事件名称。
   * @param {{x: number, y: number}} [payload] 鼠标事件的可选坐标载荷。
   * @returns {void} 不返回值。
   * @sideEffects 同步调用当前事件集合中的所有监听器。
   */
  emit(event: string, payload?: { x: number; y: number }): void {
    for (const listener of this.listeners.get(event) ?? []) listener(payload)
  }
}

describe('InputActivity', () => {
  it('drain 只返回数量和位移标量，并在读取后清零', () => {
    const hook = new FakeInputHook()
    const activity = new InputActivity(hook as never)
    expect(activity.start()).toBe(true)

    hook.emit('keydown')
    hook.emit('click')
    hook.emit('mousemove', { x: 10, y: 10 })
    hook.emit('mousemove', { x: 13, y: 14 })

    const first = activity.drain()
    expect(first.keys).toBe(1)
    expect(first.clicks).toBe(1)
    expect(first.mouseDistance).toBe(5)
    expect(activity.drain()).toEqual({ keys: 0, clicks: 0, mouseDistance: 0, lastInputAt: 0 })
  })

  it('钩子启动失败时不阻断系统 idle 降级路径', () => {
    const hook = new FakeInputHook()
    hook.failStart = true
    const activity = new InputActivity(hook as never)

    expect(activity.start()).toBe(false)
    expect(activity.drain()).toEqual({ keys: 0, clicks: 0, mouseDistance: 0, lastInputAt: 0 })
  })
})
