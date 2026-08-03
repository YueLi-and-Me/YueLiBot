/**
 * Python 后端 HTTP + WebSocket 客户端。
 *
 * 职责：
 *  · 维持到 Python WS 的连接，接收推送事件
 *  · 把 WS 事件翻译成 Electron IPC 事件推给渲染层
 *  · 提供 send / interrupt / diary / observability HTTP 方法
 *  · 断线自动重连（有限次数）
 *
 * WS 用 undici.WebSocket（已在 dependencies 里）：WHATWG API + Node 环境支持。
 *
 * ★ 事件出口抽成 EventSink 而不是直接持 BrowserWindow。
 *   这样无头集成测试能塞一个假 sink，断言「WS 推的东西真的按正确通道出去了」——
 *   否则这一段只能靠肉眼看，而它恰好是最容易串台的地方。
 */

import { WebSocket } from 'undici'
import { IPC } from '../../shared/ipc.ts'

/** 事件出口。生产实现是 BrowserWindow.webContents.send。 */
export interface EventSink {
  send(channel: string, payload: unknown): void
  isAlive(): boolean
}

/** 把 BrowserWindow 包成 EventSink。 */
export function windowSink(win: {
  isDestroyed(): boolean
  webContents: { send(channel: string, payload: unknown): void }
}): EventSink {
  return {
    send: (channel, payload) => win.webContents.send(channel, payload),
    isAlive: () => !win.isDestroyed(),
  }
}

export class PythonClient {
  private ws: WebSocket | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private stopped = false
  private reconnects = 0
  /** 握手被 401 拒绝时不再重连 —— token 不会自己变对，重连只是白烧。 */
  private authRejected = false
  private static readonly MAX_RECONNECTS = 10
  private static readonly RECONNECT_DELAY = 3_000
  /** HTTP 超时。没有它，Python 挂住会让 ipcMain.handle 永远 pending，输入栏直接卡死。 */
  private static readonly HTTP_TIMEOUT = 130_000

  constructor(
    private readonly port: number,
    private readonly token: string,
    private readonly sink: EventSink,
  ) {}

  connect(): void {
    this.stopped = false
    this.authRejected = false
    this._connect()
  }

  stop(): void {
    this.stopped = true
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    try {
      this.ws?.close()
    } catch {
      /* 已经断了 */
    }
    this.ws = null
  }

  get connected(): boolean {
    return this.ws?.readyState === 1
  }

  // ──────────────────────────────────────────────────────────────────
  // HTTP
  // ──────────────────────────────────────────────────────────────────

  async health(): Promise<boolean> {
    try {
      const res = await this._fetch('/health', { method: 'GET' })
      return res.ok
    } catch {
      return false
    }
  }

  async send(text: string): Promise<number> {
    const res = await this._fetch('/chat/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    })
    const json = (await res.json()) as { turnId?: number }
    return json.turnId ?? 0
  }

  async interrupt(): Promise<void> {
    await this._fetch('/chat/interrupt', { method: 'POST' })
  }

  async diary(): Promise<unknown> {
    return (await this._fetch('/diary', { method: 'GET' })).json()
  }

  async observability(): Promise<unknown> {
    return (await this._fetch('/observability', { method: 'GET' })).json()
  }

  async debugTrace(since: number): Promise<unknown> {
    return (await this._fetch(`/debug/trace?since=${since}`, { method: 'GET' })).json()
  }

  async foreground(info: unknown): Promise<void> {
    await this._fetch('/platform/foreground', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(info),
    })
  }

  async screenshot(jpeg: Buffer | Uint8Array): Promise<void> {
    await this._fetch('/platform/screenshot', {
      method: 'POST',
      headers: { 'Content-Type': 'image/jpeg' },
      body: jpeg as unknown as BodyInit,
    })
  }

  // ──────────────────────────────────────────────────────────────────
  // 内部
  // ──────────────────────────────────────────────────────────────────

  private async _fetch(path: string, init: RequestInit): Promise<Response> {
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), PythonClient.HTTP_TIMEOUT)
    try {
      return await fetch(`http://127.0.0.1:${this.port}${path}`, {
        ...init,
        headers: { Authorization: `Bearer ${this.token}`, ...(init.headers ?? {}) },
        signal: ctl.signal,
      })
    } finally {
      clearTimeout(timer)
    }
  }

  private _connect(): void {
    // 子协议方式传 token：标准握手字段，浏览器与 Node 实现都支持
    const ws = new WebSocket(`ws://127.0.0.1:${this.port}/ws`, [`yueli-${this.token}`])
    this.ws = ws

    ws.addEventListener('open', () => {
      this.reconnects = 0
      console.log('[python-client] WS 已连接')
    })

    ws.addEventListener('message', (event: Event) => {
      const data = (event as unknown as { data: unknown }).data
      this._handleMessage(typeof data === 'string' ? data : String(data))
    })

    ws.addEventListener('close', (event: Event) => {
      const code = (event as unknown as { code?: number }).code
      // 1008 = policy violation，服务端鉴权失败时用它关连接
      if (code === 1008) {
        this.authRejected = true
        console.error('[python-client] WS 鉴权被拒，停止重连')
      }
      if (!this.stopped && !this.authRejected) this._scheduleReconnect()
    })

    ws.addEventListener('error', (event: Event) => {
      console.warn('[python-client] WS 错误：', (event as ErrorEvent).message ?? event.type)
    })
  }

  private _handleMessage(raw: string): void {
    let msg: { channel?: string; payload?: unknown }
    try {
      msg = JSON.parse(raw)
    } catch {
      return
    }
    if (!this.sink.isAlive()) return

    switch (msg.channel) {
      case 'chat.event':
      case 'chat.done':
      case 'chat.error':
        this.sink.send(IPC.Event, msg.payload)
        break
      case 'voice.play':
        this.sink.send(IPC.Voice, msg.payload)
        break
      case 'vision.watching':
        this.sink.send(IPC.Vision, msg.payload)
        break
      case 'sleep.state':
        this.sink.send(IPC.Sleep, msg.payload)
        break
      default:
        console.debug('[python-client] 未知通道：', msg.channel)
    }
  }

  private _scheduleReconnect(): void {
    if (this.reconnects >= PythonClient.MAX_RECONNECTS) return
    this.reconnects++
    this.reconnectTimer = setTimeout(() => {
      if (!this.stopped) this._connect()
    }, PythonClient.RECONNECT_DELAY)
    this.reconnectTimer.unref?.()
  }
}
