/**
 * 与 Python 后端的连接管理。
 *
 * 职责：
 *  - 读取运行时凭据文件，健康探测后接管**已在运行**的后端；
 *  - 接管成功后周期探测存活，失联时自动重连；
 *  - 在限定时间内始终连不上时向上报告，由主进程提示用户并退出。
 *
 * 【关键】本模块只连接，不启动、不终止后端进程。
 *
 * - 现象：早先这里是 `PythonSupervisor`，找不到后端就自己 `spawn` 一个，
 *   并顺带拉起 QQ 适配器。
 * - 原因：入口已经反转——Python 是进程入口，桌面外壳由它按 `[desktop_pet] enabled`
 *   决定是否拉起，适配器也归它监护。
 * - 后果：这里若恢复 `spawn`，两侧会各拉起一个适配器（两个协议端连接同一后端，
 *   消息重复入站），桌宠开启时还会出现「后端拉起外壳、外壳又拉起后端」的第二份进程。
 *
 * 本模块不依赖 Electron API，路径完全由调用方注入，因而可在 Node 测试环境中单独验证。
 */

import { EventEmitter } from 'node:events'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'


export interface BackendConnection {
  port: number
  token: string
}

function terminalDisplayWidth(text: string): number {
  let width = 0
  for (const character of text) {
    const codePoint = character.codePointAt(0) ?? 0
    const isWide =
      (codePoint >= 0x1100 && codePoint <= 0x115f) ||
      (codePoint >= 0x2e80 && codePoint <= 0xa4cf) ||
      (codePoint >= 0xac00 && codePoint <= 0xd7a3) ||
      (codePoint >= 0xf900 && codePoint <= 0xfaff) ||
      (codePoint >= 0xfe10 && codePoint <= 0xfe6f) ||
      (codePoint >= 0xff00 && codePoint <= 0xff60) ||
      (codePoint >= 0xffe0 && codePoint <= 0xffe6)
    width += isWide ? 2 : 1
  }
  return width
}

function padDisplayWidth(text: string, width: number): string {
  return text + ' '.repeat(Math.max(width - terminalDisplayWidth(text), 0))
}

/**
 * 把已连接后端的坐标渲染成启动时可快速扫读的信息框。
 *
 * Python 自己也会输出一个同样内容的入口框，但那是在它的终端里；桌面外壳单独启动时
 * 用户只看得到 Electron 这边的输出，没有这个框就只剩一行端口提示、找不到 token。
 */
function renderBackendEntryBox(connection: BackendConnection, title: string): string {
  const rows = [
    `WebUI 观察面板：http://127.0.0.1:${connection.port}`,
    `登录 token：${connection.token}`,
    title,
  ]
  const contentWidth = Math.max(76, ...rows.map(terminalDisplayWidth), terminalDisplayWidth(title))
  const panelWidth = contentWidth + 4
  const topPrefix = `╭─ YueLiBot · WebUI 入口 `
  const top = `${topPrefix}${'─'.repeat(Math.max(panelWidth - terminalDisplayWidth(topPrefix) - 1, 0))}╮`
  const body = rows.map((row) => `│ ${padDisplayWidth(row, contentWidth)} │`)
  const bottom = `╰${'─'.repeat(panelWidth - 2)}╯`
  return [top, ...body, bottom].join('\n')
}

export interface BackendLinkEvents {
  /** 已连上后端，携带连接坐标。后端重启后会再次发出。 */
  ready: [port: number, token: string]
  /** 与后端失联，正在重连；下游据此停掉旧客户端。 */
  lost: []
  /** 在限定时间内始终连不上。不发这个事件的话主进程会一直停在空白窗口上。 */
  unavailable: [error: Error]
}

export interface BackendLinkOptions {
  /** memory.db 所在目录；运行时凭据文件在它的 runtime 子目录下。 */
  dataDir: string
  /** 首次连接与重连的最长等待毫秒数；省略时使用默认值。 */
  attachTimeout?: number
}

export class BackendLink extends EventEmitter<BackendLinkEvents> {
  private connection: BackendConnection | null = null
  private monitor: ReturnType<typeof setInterval> | null = null
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private stopping = false
  private connecting = false
  /** 本轮连接尝试的截止时刻；null 表示当前没有进行中的尝试。 */
  private deadline: number | null = null
  /** 连接失败后的重试间隔。后端启动到监听通常在秒级，无需指数退避。 */
  private static readonly RETRY_INTERVAL = 1_000
  /** 存活探测间隔；请求本身另有短超时。 */
  private static readonly PROBE_INTERVAL = 5_000
  /** 健康探测的单次超时。 */
  private static readonly PROBE_TIMEOUT = 1_500
  /** 连接尝试的默认总时长：覆盖后端重启（优雅收尾最多约 15 秒）后重新监听。 */
  private static readonly DEFAULT_ATTACH_TIMEOUT = 30_000

  private readonly dataDir: string
  private readonly attachTimeout: number

  /**
   * 创建后端连接管理器。
   *
   * @param opts 数据目录与可选的连接超时；构造阶段不读文件、不发请求。
   */
  constructor(opts: BackendLinkOptions) {
    super()
    this.dataDir = opts.dataDir
    this.attachTimeout = opts.attachTimeout ?? BackendLink.DEFAULT_ATTACH_TIMEOUT
  }

  /**
   * 开始连接后端。
   *
   * @returns 无返回值；结果通过 ready / unavailable 事件发出。
   * @sideEffects 读取 dataDir/runtime/backend.json 并周期性发起本机健康请求。
   */
  start(): void {
    this.stopping = false
    this.deadline = Date.now() + this.attachTimeout
    void this.attempt()
  }

  /**
   * 停止连接管理。
   *
   * @returns 无返回值；重复调用安全。
   * @sideEffects 取消存活探测与重试定时器；不影响后端进程本身。
   */
  stop(): void {
    this.stopping = true
    this.connection = null
    this.deadline = null
    this.clearMonitor()
    this.clearRetry()
  }

  /**
   * 判断当前是否持有一个可用的后端连接。
   *
   * @returns {boolean} 已完成一次成功接管且尚未失联时为 ``true``。
   */
  get alive(): boolean {
    return this.connection !== null
  }

  /**
   * 尝试一次连接；失败则安排重试，超过截止时刻则报告不可用。
   *
   * @returns 完成一次尝试后的 Promise。
   * @sideEffects 可能发出 ready 或 unavailable 事件，并注册重试定时器。
   */
  private async attempt(): Promise<void> {
    if (this.stopping || this.connecting) return
    this.connecting = true
    try {
      const existing = await this.readRuntimeConnection()
      if (this.stopping) return
      if (existing !== null && await this.probe(existing)) {
        this.attach(existing)
        return
      }
      if (this.deadline !== null && Date.now() >= this.deadline) {
        this.deadline = null
        this.emit('unavailable', new Error(
          '未找到正在运行的 Python 后端。'
          + '本应用的进程入口是 Python：请先运行 python bot.py --data-dir data --config-path config，'
          + '或让它按 [desktop_pet] enabled 自动拉起桌宠。',
        ))
        return
      }
      this.scheduleRetry()
    } finally {
      this.connecting = false
    }
  }

  /**
   * 安排下一次连接尝试。
   *
   * @returns 无返回值；已在停止流程中时不注册定时器。
   * @sideEffects 创建不阻止进程退出的一次性定时器。
   */
  private scheduleRetry(): void {
    if (this.stopping) return
    this.clearRetry()
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null
      void this.attempt()
    }, BackendLink.RETRY_INTERVAL)
    this.retryTimer.unref?.()
  }

  /**
   * 读取运行时连接文件并校验字段。
   *
   * @returns 端口和 64 位十六进制令牌均有效时返回连接信息，否则返回 ``null``。
   * @sideEffects 读取 dataDir/runtime/backend.json。
   */
  private async readRuntimeConnection(): Promise<BackendConnection | null> {
    const runtimePath = join(this.dataDir, 'runtime', 'backend.json')
    let payload: unknown
    try {
      payload = JSON.parse(await readFile(runtimePath, 'utf8'))
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code
      // 后端还没起来时文件本就不存在，这条路径每秒走一次，不值得刷屏。
      if (code !== 'ENOENT') {
        console.warn(`[backend] 后端运行时文件不可用：${String(error)}`)
      }
      return null
    }
    if (!payload || typeof payload !== 'object') {
      console.warn('[backend] 后端运行时文件不是 JSON 对象')
      return null
    }
    const port = (payload as { port?: unknown }).port
    const token = (payload as { token?: unknown }).token
    if (
      typeof port !== 'number'
      || !Number.isInteger(port)
      || port <= 0
      || port > 65535
      || typeof token !== 'string'
      || !/^[0-9a-f]{64}$/.test(token)
    ) {
      console.warn('[backend] 后端运行时文件字段无效')
      return null
    }
    return { port, token }
  }

  /**
   * 使用短超时和令牌探测后端运行状态。
   *
   * @param connection 待探测的本机端口和鉴权令牌。
   * @returns HTTP 响应成功且正文可读取时为 ``true``；网络、超时或非 2xx 时为 ``false``。
   */
  private async probe(connection: BackendConnection): Promise<boolean> {
    try {
      const response = await fetch(`http://127.0.0.1:${connection.port}/runtime/health`, {
        headers: { Authorization: `Bearer ${connection.token}` },
        signal: AbortSignal.timeout(BackendLink.PROBE_TIMEOUT),
      })
      await response.arrayBuffer()
      return response.ok
    } catch {
      return false
    }
  }

  /**
   * 记录连接并向下游发布 ready 事件。
   *
   * @param connection 已通过健康检查的连接信息。
   * @returns 无返回值。
   * @sideEffects 保存连接、启动存活监控并发出 ready 事件。
   */
  private attach(connection: BackendConnection): void {
    this.connection = connection
    this.deadline = null
    this.clearRetry()
    console.log(`[backend] 已连接 Python 后端，端口 ${connection.port}`)
    console.log(renderBackendEntryBox(connection, '状态：已连接正在运行的后端'))
    this.emit('ready', connection.port, connection.token)
    this.startMonitor()
  }

  /**
   * 启动后端存活的周期检查。
   *
   * @returns 无返回值；重复启动前会先清除旧定时器。
   * @sideEffects 创建不阻止进程退出的轮询定时器。
   */
  private startMonitor(): void {
    this.clearMonitor()
    this.monitor = setInterval(() => {
      void this.checkAlive()
    }, BackendLink.PROBE_INTERVAL)
    this.monitor.unref?.()
  }

  /**
   * 检查当前后端，失联时发出 lost 并重新进入连接流程。
   *
   * 后端自己重启（`/system/restart` 之后重新执行入口）时会走这条路径，因此失联
   * 不等于结束：重新给一整个连接窗口，连不上才报不可用。
   *
   * @returns 完成一次健康检查后的 Promise。
   * @sideEffects 可能清空连接、停止监控、发出 lost 并触发下一次连接尝试。
   */
  private async checkAlive(): Promise<void> {
    const connection = this.connection
    if (this.stopping || connection === null) return
    if (await this.probe(connection)) return
    if (this.connection !== connection) return
    this.connection = null
    this.clearMonitor()
    console.warn('[backend] 与 Python 后端失联，正在重连')
    this.emit('lost')
    this.deadline = Date.now() + this.attachTimeout
    void this.attempt()
  }

  /**
   * 清除存活监控定时器。
   *
   * @returns 无返回值；没有定时器时安全返回。
   */
  private clearMonitor(): void {
    if (this.monitor === null) return
    clearInterval(this.monitor)
    this.monitor = null
  }

  /**
   * 清除重试定时器。
   *
   * @returns 无返回值；没有定时器时安全返回。
   */
  private clearRetry(): void {
    if (this.retryTimer === null) return
    clearTimeout(this.retryTimer)
    this.retryTimer = null
  }
}
