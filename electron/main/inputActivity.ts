/**
 * 全局键鼠活动聚合器。
 *
 * 隐私红线：这里只累计按键数、点击数、鼠标位移标量和最后输入时间，绝不记录
 * keycode、字符、窗口内容或坐标原文。鼠标坐标只在单次移动事件里计算距离后丢弃。
 */

import { uIOhook, type UiohookMouseEvent } from 'uiohook-napi'

export interface InputActivitySnapshot {
  keys: number
  clicks: number
  mouseDistance: number
  lastInputAt: number
}

interface InputHook {
  on(event: 'keydown', listener: () => void): unknown
  on(event: 'click', listener: () => void): unknown
  on(event: 'mousemove', listener: (event: UiohookMouseEvent) => void): unknown
  removeListener(event: 'keydown', listener: () => void): unknown
  removeListener(event: 'click', listener: () => void): unknown
  removeListener(event: 'mousemove', listener: (event: UiohookMouseEvent) => void): unknown
  start(): void
  stop(): void
}

/** 全局钩子按现有前台轮询窗口聚合；drain 后立即清空，所以不会无界增长。 */
export class InputActivity {
  private keys = 0
  private clicks = 0
  private mouseDistance = 0
  private lastInputAt = 0
  private lastPointer: { x: number; y: number } | null = null
  private started = false

  private readonly onKeydown = () => {
    this.keys++
    this.lastInputAt = Date.now()
  }

  private readonly onClick = () => {
    this.clicks++
    this.lastInputAt = Date.now()
  }

  private readonly onMousemove = (event: UiohookMouseEvent) => {
    if (this.lastPointer) {
      this.mouseDistance += Math.hypot(event.x - this.lastPointer.x, event.y - this.lastPointer.y)
    }
    // 仅保留下一次计算距离所需的瞬时点，不将坐标写入快照、日志或任何持久化位置。
    this.lastPointer = { x: event.x, y: event.y }
    this.lastInputAt = Date.now()
  }

  /**
   * 创建输入活动聚合器。
   *
   * @param hook 全局输入钩子实现，默认使用 uIOhook；测试可注入兼容实现。
   * @throws 不在构造阶段启动钩子，因此不会因权限错误抛出异常。
   */
  constructor(private readonly hook: InputHook = uIOhook) {}

  /**
   * 注册键盘、鼠标事件并启动全局输入钩子。
   *
   * @returns 已启动或重复调用时返回 ``true``；权限、驱动或安全软件阻止时返回
   * ``false``，调用方应回退到系统空闲时间。
   * @sideEffects 注册事件监听器并开始累计数量、位移和最后输入时间；失败时撤销
   * 已注册监听器。
   */
  start(): boolean {
    if (this.started) return true
    try {
      this.hook.on('keydown', this.onKeydown)
      this.hook.on('click', this.onClick)
      this.hook.on('mousemove', this.onMousemove)
      this.hook.start()
      this.started = true
      console.info('[input-activity] 全局键鼠聚合已启动：只读取数量与位移标量')
      return true
    } catch (error) {
      this.hook.removeListener('keydown', this.onKeydown)
      this.hook.removeListener('click', this.onClick)
      this.hook.removeListener('mousemove', this.onMousemove)
      console.warn('[input-activity] 全局键鼠钩子不可用，将只使用系统空闲时间：', error)
      return false
    }
  }

  /**
   * 停止输入钩子、移除监听器并清空当前累计快照。
   *
   * @returns 无返回值；未启动时不执行任何操作。
   * @sideEffects 停止全局钩子并重置键数、点击数、位移、时间和瞬时指针。
   */
  stop(): void {
    if (!this.started) return
    this.hook.removeListener('keydown', this.onKeydown)
    this.hook.removeListener('click', this.onClick)
    this.hook.removeListener('mousemove', this.onMousemove)
    this.hook.stop()
    this.started = false
    this.reset()
  }

  /**
   * 读取当前窗口周期的输入活动并立即开始下一周期累计。
   *
   * @returns 包含按键数、点击数、鼠标位移和最后输入时间的快照；读取后内部值
   * 全部归零。
   * @sideEffects 清空当前计数，不影响钩子是否仍在运行。
   */
  drain(): InputActivitySnapshot {
    const snapshot = {
      keys: this.keys,
      clicks: this.clicks,
      mouseDistance: this.mouseDistance,
      lastInputAt: this.lastInputAt,
    }
    this.reset()
    return snapshot
  }

  /**
   * 清除累计值和用于计算下一次鼠标位移的瞬时坐标。
   *
   * @returns 无返回值。
   * @sideEffects 重置本实例的内部计数和指针状态。
   */
  private reset(): void {
    this.keys = 0
    this.clicks = 0
    this.mouseDistance = 0
    this.lastInputAt = 0
    this.lastPointer = null
  }
}
