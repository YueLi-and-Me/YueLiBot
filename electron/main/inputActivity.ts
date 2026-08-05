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

  constructor(private readonly hook: InputHook = uIOhook) {}

  /** 返回 false 表示权限或安全软件阻止了钩子；调用者仍可用系统 idle 时间工作。 */
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

  stop(): void {
    if (!this.started) return
    this.hook.removeListener('keydown', this.onKeydown)
    this.hook.removeListener('click', this.onClick)
    this.hook.removeListener('mousemove', this.onMousemove)
    this.hook.stop()
    this.started = false
    this.reset()
  }

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

  private reset(): void {
    this.keys = 0
    this.clicks = 0
    this.mouseDistance = 0
    this.lastInputAt = 0
    this.lastPointer = null
  }
}
