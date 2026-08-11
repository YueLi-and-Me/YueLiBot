/**
 * Python 后端 HTTP + WebSocket 客户端。
 *
 * 职责：
 *  - 维持到 Python WebSocket 的连接并接收推送事件
 *  - 将后端事件转换为 Electron IPC 事件并发送给渲染层
 *  - 提供聊天、日记、观察、前台活动和截图 HTTP 方法
 *  - 在非鉴权失败场景下执行有限次数的断线重连
 *
 * WS 用 undici.WebSocket（已在 dependencies 里）：WHATWG API + Node 环境支持。
 *
 * 事件出口抽象为 EventSink，使无头测试可以验证通道映射，同时避免客户端直接依赖
 * BrowserWindow。
 */

import { WebSocket } from 'undici'
import { IPC } from '../../shared/ipc.ts'

/** 事件出口。生产实现是 BrowserWindow.webContents.send。 */
export interface EventSink {
  /** 向渲染层发送指定 IPC 通道和载荷。 */
  send(channel: string, payload: unknown): void
  /** 返回接收窗口是否仍可发送事件。 */
  isAlive(): boolean
}

/**
 * 将具有 webContents 的窗口适配为 EventSink。
 *
 * @param win 提供 isDestroyed 和 webContents.send 的窗口对象。
 * @returns {EventSink} 将消息发送到窗口 webContents，并按窗口销毁状态报告可用性的事件出口。
 * @throws Error webContents.send 在窗口关闭期间拒绝发送时由 Electron 传播。
 * @sideEffects 不创建窗口；调用返回对象的 send 方法时会向渲染层发送 IPC 消息。
 */
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
  /** 连接成功过才算进入重连阶段；握手失败仍归为首次连接阶段。 */
  private hasConnected = false
  /** 握手被 401 拒绝时不再重连 —— token 不会自己变对，重连只是白烧。 */
  private authRejected = false
  private static readonly MAX_RECONNECTS = 10
  private static readonly RECONNECT_DELAY = 3_000
  /** HTTP 超时。没有它，Python 挂住会让 ipcMain.handle 永远 pending，输入栏直接卡死。 */
  private static readonly HTTP_TIMEOUT = 130_000
  /**
   * 聊天截图请求的客户端超时，必须大于后端视觉截止时间，避免客户端先中止请求
   * 导致后端将正常的视觉截止转换为取消异常。
   */
  private static readonly CHAT_GLANCE_TIMEOUT = 20_000

  /**
   * 创建后端 HTTP/WebSocket 客户端。
   *
   * @param port 后端监听的本机 TCP 端口，必须为正整数。
   * @param token 后端 HTTP 和 WebSocket 鉴权令牌，不写入日志或事件 payload。
   * @param sink 渲染层事件出口。
   * @param onCaptureRequest 后端请求截图时的回调；省略时忽略截图请求。
   */
  constructor(
    private readonly port: number,
    private readonly token: string,
    private readonly sink: EventSink,
    private readonly onCaptureRequest?: (reason: string) => void,
  ) {}

  /**
   * 开始建立后端 WebSocket 连接。
   *
   * @returns 无返回值；连接结果通过 ``connected``、事件出口和重连逻辑体现。
   * @sideEffects 清除停止和鉴权拒绝状态并创建 WebSocket；不会阻塞调用方。
   */
  connect(): void {
    this.stopped = false
    this.authRejected = false
    this.hasConnected = false
    this._connect()
  }

  /**
   * 停止 WebSocket 客户端并取消待执行的重连任务。
   *
   * @returns 无返回值；重复调用安全。
   * @sideEffects 关闭当前 WebSocket、清除重连定时器并阻止后续自动重连。
   */
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

  /**
   * 返回当前 WebSocket 是否处于 OPEN 状态。
   *
   * @returns {boolean} 连接存在且 readyState 为 ``OPEN`` 时返回 ``true``，否则返回 ``false``。
   */
  get connected(): boolean {
    return this.ws?.readyState === 1
  }

  // ──────────────────────────────────────────────────────────────────
  // HTTP
  // ──────────────────────────────────────────────────────────────────

  /**
   * 查询后端健康检查接口。
   *
   * @returns HTTP 返回成功时为 ``true``；网络错误、超时或非 2xx 时为 ``false``。
   */
  async health(): Promise<boolean> {
    try {
      const res = await this._fetch('/health', { method: 'GET' })
      return res.ok
    } catch {
      return false
    }
  }

  /**
   * 提交一条聊天文本并返回后端分配的回合 ID。
   *
   * @param text 待发送的 UTF-8 文本；空值校验由后端接口执行。
   * @returns 后端返回的回合 ID；响应缺少该字段时返回 ``0``。
   * @throws Error 网络、超时或后端返回不可解析 JSON 时抛出。
   */
  async send(text: string): Promise<number> {
    const res = await this._fetch('/chat/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    })
    const json = (await res.json()) as { turnId?: number }
    return json.turnId ?? 0
  }

  /**
   * 请求后端中断当前聊天回合。
   *
   * @returns HTTP 请求完成后的 Promise。
   * @throws Error 网络、超时或后端返回失败时抛出。
   */
  async interrupt(): Promise<void> {
    await this._fetch('/chat/interrupt', { method: 'POST' })
  }

  /**
   * 获取后端生成的日记面板数据。
   *
   * @returns 经过 JSON 解码的日记 payload。
   * @throws Error 网络、超时或响应不是合法 JSON 时抛出。
   */
  async diary(): Promise<unknown> {
    return (await this._fetch('/diary', { method: 'GET' })).json()
  }

  /**
   * 获取默认桌面 stream 的观察快照。
   *
   * @returns 经过 JSON 解码的观察 payload。
   * @throws Error 网络、超时或响应不是合法 JSON 时抛出。
   */
  async observability(): Promise<unknown> {
    return (await this._fetch('/observability?streamId=1', { method: 'GET' })).json()
  }

  /**
   * 获取指定序号之后的追踪事件。
   *
   * @param since 起始事件序号，传入 ``0`` 表示从当前保留窗口开始读取。
   * @returns 经过 JSON 解码的追踪 payload。
   * @throws Error 网络、超时或响应不是合法 JSON 时抛出。
   */
  async debugTrace(since: number): Promise<unknown> {
    return (await this._fetch(`/debug/trace?since=${since}`, { method: 'GET' })).json()
  }

  /**
   * 提交前台活动快照供后端感知服务更新。
   *
   * @param info 前台采集模块产生的可序列化活动对象。
   * @returns HTTP 请求完成后的 Promise。
   * @throws Error 网络、超时或后端返回失败时抛出。
   */
  async foreground(info: unknown): Promise<void> {
    await this._fetch('/platform/foreground', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(info),
    })
  }

  /**
   * 提交聊天触发的单帧 JPEG，并等待后端视觉描述完成。
   *
   * @param jpeg JPEG 二进制内容，支持 Node Buffer 或 Uint8Array。
   * @returns 视觉请求完成后的 Promise。
   * @throws Error 网络、超时或后端拒绝图片时抛出。
   * @sideEffects 后端可能更新当前视觉描述；客户端不保存图片内容。
   */
  async screenshotChat(jpeg: Buffer | Uint8Array): Promise<void> {
    await this._fetch('/platform/screenshot/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'image/jpeg' },
      body: jpeg as unknown as BodyInit,
    }, PythonClient.CHAT_GLANCE_TIMEOUT)
  }

  // ──────────────────────────────────────────────────────────────────
  // 内部
  // ──────────────────────────────────────────────────────────────────

  /**
   * 为本机后端 HTTP 请求注入鉴权头和超时控制。
   *
   * @param path 后端相对路径，必须以 ``/`` 开头。
   * @param init fetch 请求方法、请求头和正文配置。
   * @param timeoutMs 超时毫秒数，默认使用普通 HTTP 超时。
   * @returns fetch 返回的 Response；调用方负责读取正文和检查状态。
   * @throws TypeError 网络失败；AbortError 请求超过超时；其他 fetch 错误直接传播。
   */
  private async _fetch(path: string, init: RequestInit, timeoutMs: number = PythonClient.HTTP_TIMEOUT): Promise<Response> {
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), timeoutMs)
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

  /**
   * 创建一次 WebSocket 连接并注册生命周期与消息映射处理器。
   *
   * @returns 无返回值；握手和消息处理均由 WebSocket 事件异步完成。
   * @sideEffects 写入当前连接引用、更新连接阶段状态、转发事件并按关闭原因安排
   * 重连；鉴权失败不会重连。
   */
  private _connect(): void {
    // 子协议方式传 token：标准握手字段，浏览器与 Node 实现都支持
    const ws = new WebSocket(`ws://127.0.0.1:${this.port}/ws?client=desktop`, [`yueli-${this.token}`])
    this.ws = ws

    ws.addEventListener('open', () => {
      this.hasConnected = true
      this.reconnects = 0
      console.log('[python-client] WS 已连接')
    })

    ws.addEventListener('message', (event: Event) => {
      const data = (event as unknown as { data: unknown }).data
      this._handleMessage(typeof data === 'string' ? data : String(data))
    })

    ws.addEventListener('close', (event: Event) => {
      const closeEvent = event as unknown as { code?: number; reason?: string }
      // 主动 stop 产生的 close 不应进入故障告警或重连流程。
      if (!this.stopped) {
        const detail = [`code=${closeEvent.code ?? 'unknown'}`]
        const reason = typeof closeEvent.reason === 'string' ? closeEvent.reason.trim() : ''
        if (reason) detail.push(`reason=${reason}`)
        const phase = this.hasConnected ? '重连' : '首次连接'
        console.warn(`[python-client] WS ${phase}关闭：`, detail.join(' '))
      }

      // 1008 表示策略拒绝；后端以此报告鉴权失败，令牌未变更时重连没有意义。
      if (closeEvent.code === 1008) {
        this.authRejected = true
        console.error('[python-client] WS 鉴权被拒，停止重连')
      }
      if (!this.stopped && !this.authRejected) this._scheduleReconnect()
    })

    // error 事件不携带可诊断原因，具体 code/reason 由 close 事件记录；仍需监听以
    // 防止 EventTarget 将其视为未处理错误。
    ws.addEventListener('error', () => {})
  }

  /**
   * 解析后端 WebSocket 消息并映射到截图回调或渲染层 IPC 通道。
   *
   * @param raw WebSocket 收到的文本消息。
   * @returns 无返回值；非法 JSON、未知通道或已失活渲染窗口直接丢弃。
   * @sideEffects 可能触发截图回调，或通过 EventSink 发送聊天、语音、视觉和睡眠事件。
   */
  private _handleMessage(raw: string): void {
    let msg: { channel?: string; payload?: unknown }
    try {
      msg = JSON.parse(raw)
    } catch {
      return
    }
    // 截图请求发给主进程而非渲染层；即使窗口隐藏，也必须先处理该请求。
    if (msg.channel === 'vision.capture_request') {
      const payload = msg.payload as { reason?: unknown } | undefined
      this.onCaptureRequest?.(typeof payload?.reason === 'string' ? payload.reason : '')
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

  /**
   * 在达到重连上限前安排一次延迟 WebSocket 重连。
   *
   * @returns 无返回值；达到上限时不创建定时器。
   * @sideEffects 增加重连计数并注册可取消定时器；客户端停止后回调不再连接。
   */
  private _scheduleReconnect(): void {
    if (this.reconnects >= PythonClient.MAX_RECONNECTS) return
    this.reconnects++
    this.reconnectTimer = setTimeout(() => {
      if (!this.stopped) this._connect()
    }, PythonClient.RECONNECT_DELAY)
    this.reconnectTimer.unref?.()
  }
}
