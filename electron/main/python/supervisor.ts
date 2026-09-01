/**
 * Python 后端进程监护。
 *
 * 职责：
 *  - 按调用方提供的工作目录启动 ``bot.py``，解析端口、令牌和就绪信号。
 *  - 将标准输出和错误输出转发到 Electron 控制台，并在异常退出后有限退避重启。
 *  - 监控已由其他进程启动的后端，必要时切换回本地子进程。
 *  - 关闭时终止后端及可选的 QQ 适配器进程树。
 *
 * 本模块不依赖 Electron API，路径完全由调用方注入，因而可在 Node 测试环境中
 * 单独验证进程和就绪信号处理。
 */

import { execFile, spawn } from 'node:child_process'
import type { ChildProcess } from 'node:child_process'
import { EventEmitter } from 'node:events'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'


interface BackendConnection {
  port: number
  token: string
}

/** 结束一个子进程及其整棵进程树。

 * Windows 上优先 taskkill /T；taskkill 偶尔迟迟不返回，不能让已知的 Python
 * 父进程一直活着阻塞 Electron 退出，因此保留 2 秒后的直接终止兜底。
 * execFile 永不经由 shell 解释参数；pid 值先通过纯数字校验再进入参数表，
 * 杜绝被目标程序解释为选项前缀的可能，异常形态直接走直杀兜底。
 *
 * @param target 要结束的子进程。
 * @sideEffects 启动 taskkill 子进程或直接终止目标进程。
 */
function stopProcessTree(target: ChildProcess): void {
  const directKill = () => {
    if (target.exitCode === null) target.kill()
  }
  const pidText = String(target.pid ?? '')
  if (process.platform !== 'win32' || !/^\d+$/.test(pidText)) {
    directKill()
    return
  }
  const fallback = setTimeout(directKill, 2_000)
  const taskkill = execFile('taskkill', ['/T', '/F', '/pid', pidText], () => {})
  const finish = () => {
    clearTimeout(fallback)
    directKill()
  }
  taskkill.on('error', finish)
  taskkill.on('close', finish)
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
 * 把已存在后端的连接坐标渲染成启动时可快速扫读的信息框。
 *
 * 本地新后端会由 Python 侧输出同样的入口框；这里覆盖“接管已运行后端”路径，
 * 避免 Electron 重启时只剩一行端口提示而找不到 token。
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

export interface SupervisorEvents {
  ready: [port: number, token: string]
  exit: [code: number | null]
  /** 拉不起来（python 不存在、权限不足）。不发这个事件的话 error 会成为未捕获异常。 */
  failed: [error: Error]
  /** QQ 适配器异常退出；由主进程负责把这个故障呈现到托盘。 */
  adapterFailed: [error: Error]
}

export interface SupervisorOptions {
  /** memory.db 所在目录。 */
  dataDir: string
  /** 配置目录的完整路径；Python 加载器也兼容迁移前的单文件路径。 */
  configPath: string
  /** Python 侧的工作目录：含 bot.py 与 src/ 的那一层，也就是仓库根。 */
  cwd: string
  pythonExe?: string
  /** QQ 适配器配置文件；不传表示只启动主体，便于保留无 QQ 场景。 */
  napcatConfigPath?: string
}

/**
 * 将子进程退出码和信号转换为稳定的诊断文本。
 *
 * @param code Node 提供的退出码；正常由信号结束时可为 ``null``。
 * @param signal Node 提供的终止信号；正常退出时为 ``null``。
 * @returns 包含可用退出字段的空格分隔文本；两者均为空时返回“无退出详情”。
 */
export function formatProcessExitDetails(
  code: number | null,
  signal: NodeJS.Signals | null,
): string {
  const details: string[] = []
  if (code !== null) details.push(`code=${code}`)
  if (signal) details.push(`signal=${signal}`)
  return details.length > 0 ? details.join(' ') : '无退出详情'
}

export class PythonSupervisor extends EventEmitter<SupervisorEvents> {
  private child: ChildProcess | null = null
  private adapter: ChildProcess | null = null
  private restarts = 0
  private stopping = false
  private connecting = false
  private attachedBackend: BackendConnection | null = null
  private attachedMonitor: ReturnType<typeof setInterval> | null = null
  /** stdout 行缓冲。网络/管道分包与行边界无关，不缓冲会漏掉半截公告。 */
  private stdoutBuf = ''
  private adapterStdoutBuf = ''
  private adapterStderrBuf = ''
  /** 后端启动公告集合；三项齐全后才能把连接坐标交给下游。 */
  private backendPort: number | null = null
  private backendToken: string | null = null
  private backendReady = false
  private readyEmitted = false
  /** 进行中的优雅关闭流程；缓存 Promise 使重复调用幂等，start() 时清零。 */
  private _shutdownPromise: Promise<void> | null = null
  /** 最大重启次数。超过后等用户重启 Electron。 */
  private static readonly MAX_RESTARTS = 5
  /** 退避基准（毫秒）。 */
  private static readonly BASE_BACKOFF = 2_000
  /** 稳定运行超过这个时长就认为上次重启成功，清零计数。 */
  private static readonly STABLE_AFTER = 60_000
  /** 外部后端存活探测间隔；请求本身另有短超时。 */
  private static readonly ATTACHED_PROBE_INTERVAL = 5_000

  private readonly dataDir: string
  private readonly configPath: string
  private readonly cwd: string
  private readonly pythonExe: string
  private readonly napcatConfigPath: string | null

  /**
   * 创建后端进程监护器。
   *
   * @param opts 数据目录、配置路径、工作目录及可选 Python/适配器路径。
   * @throws 不在构造阶段访问文件或启动进程；参数错误在启动时由对应操作报告。
   */
  constructor(opts: SupervisorOptions) {
    super()
    this.dataDir = opts.dataDir
    this.configPath = opts.configPath
    this.cwd = opts.cwd
    this.pythonExe = opts.pythonExe ?? 'python'
    this.napcatConfigPath = opts.napcatConfigPath ?? null
  }

  /**
   * 启动或接管后端，并异步等待就绪信号。
   *
   * @returns 无返回值；就绪、退出和失败通过事件发出。
   * @sideEffects 读取运行时连接文件、可能启动 Python/适配器子进程并注册监控定时器。
   */
  start(): void {
    this.stopping = false
    this._shutdownPromise = null
    void this._connectOrSpawn()
  }

  /**
   * 停止后端监护并终止相关进程。
   *
   * @returns 无返回值；重复调用安全。
   * @sideEffects 取消外部后端监控、终止适配器和 Python 进程，并阻止后续重启。
   */
  stop(): void {
    this.stopping = true
    this.attachedBackend = null
    this._clearAttachedMonitor()
    this._killAdapter()
    this._kill()
  }

  /**
   * 优雅关闭后端：先请 Python 自行收尾，超时再回退强杀。
   *
   * 本地子进程路径：杀掉适配器（无持久状态，避免它在后端关闭期间继续提交入站
   * 消息）后 POST ``/runtime/shutdown``，等待子进程退出事件至多 ``graceMs``，
   * 超时回退 ``stopProcessTree``。接管的外部后端维持现状语义：只断开监控、
   * 杀适配器，不动外部进程。
   *
   * @param graceMs 等待子进程优雅退出的最长毫秒数。
   * @returns 关闭流程完成后的 Promise；重复调用返回同一次流程，幂等。
   * @sideEffects 终止适配器、请求后端优雅退出、必要时强杀子进程树，并阻止后续重启。
   */
  shutdown(graceMs = 10_000): Promise<void> {
    this._shutdownPromise ??= this._shutdown(graceMs)
    return this._shutdownPromise
  }

  /** ``shutdown`` 的单次执行体；重复调用由缓存的 Promise 去重。 */
  private async _shutdown(graceMs: number): Promise<void> {
    this.stopping = true
    this._clearAttachedMonitor()
    this._killAdapter()
    if (this.attachedBackend !== null) {
      this.attachedBackend = null
      return
    }
    const child = this.child
    if (!child || child.exitCode !== null) return
    if (this.backendPort !== null && this.backendToken !== null) {
      try {
        await fetch(`http://127.0.0.1:${this.backendPort}/runtime/shutdown`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${this.backendToken}` },
          signal: AbortSignal.timeout(2_000),
        })
      } catch { /* 请求失败静默：后续有强杀兜底。 */ }
    }
    // 后端可能在 shutdown 请求返回前已经退出；此时 exit 事件已发生，继续监听只会
    // 白等完整宽限时间。同步检查与监听注册之间没有 await，不会再错过事件。
    if (child.exitCode !== null) return
    const exited = await new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => {
        child.off('exit', onExit)
        resolve(false)
      }, graceMs)
      const onExit = () => {
        clearTimeout(timer)
        resolve(true)
      }
      child.once('exit', onExit)
    })
    if (!exited && child.exitCode === null) {
      console.warn('[supervisor] 优雅关闭超时，回退强制终止 Python 后端')
      stopProcessTree(child)
    }
  }

  /**
   * 判断主体后端是否仍由本监护器接管且处于可用进程状态。
   *
   * @returns {boolean} 已接管外部后端，或本地子进程存在且尚未退出时返回 ``true``。
   */
  get alive(): boolean {
    return this.attachedBackend !== null
      || (!!this.child && this.child.exitCode === null && !this.child.killed)
  }

  /** 优先连接用户独立启动的后端；确认不可用后才拉起自己的子进程。 */
  /**
   * 优先接管可用的外部后端，确认不存在后再启动本地后端。
   *
   * @returns 完成一次接管或启动尝试后的 Promise。
   * @sideEffects 更新连接状态并可能触发 ready、exit 或 failed 事件。
   */
  private async _connectOrSpawn(): Promise<void> {
    if (this.stopping || this.connecting) return
    this.connecting = true
    try {
      const existing = await this._findExistingBackend()
      if (this.stopping) return
      if (existing) {
        this._attachBackend(existing)
        return
      }
      this._spawn()
    } finally {
      this.connecting = false
    }
  }

  /**
   * 读取运行时连接文件并验证外部后端健康状态。
   *
   * @returns 端口和 64 位十六进制令牌均有效且健康检查成功时返回连接信息，否则
   * 返回 ``null``。
   * @sideEffects 读取 dataDir/runtime/backend.json 并执行一次本机健康请求。
   */
  private async _findExistingBackend(): Promise<BackendConnection | null> {
    const runtimePath = join(this.dataDir, 'runtime', 'backend.json')
    let payload: unknown
    try {
      payload = JSON.parse(await readFile(runtimePath, 'utf8'))
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code
      if (code !== 'ENOENT') {
        console.warn(`[supervisor] 后端运行时文件不可用，将尝试拉起新后端：${String(error)}`)
      }
      return null
    }
    if (!payload || typeof payload !== 'object') {
      console.warn('[supervisor] 后端运行时文件不是 JSON 对象，将尝试拉起新后端')
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
      console.warn('[supervisor] 后端运行时文件字段无效，将尝试拉起新后端')
      return null
    }
    const connection = { port, token }
    return await this._probeBackend(connection) ? connection : null
  }

  /**
   * 使用短超时和令牌探测后端运行状态。
   *
   * @param connection 待探测的本机端口和鉴权令牌。
   * @returns HTTP 响应成功且正文可读取时为 ``true``；网络、超时或非 2xx 时为
   * ``false``。
   */
  private async _probeBackend(connection: BackendConnection): Promise<boolean> {
    try {
      const response = await fetch(`http://127.0.0.1:${connection.port}/runtime/health`, {
        headers: { Authorization: `Bearer ${connection.token}` },
        signal: AbortSignal.timeout(1_500),
      })
      await response.arrayBuffer()
      return response.ok
    } catch {
      return false
    }
  }

  /**
   * 接管已运行后端并向下游发布 ready 事件。
   *
   * @param connection 已通过健康检查的连接信息。
   * @returns 无返回值。
   * @sideEffects 保存外部连接、启动适配器和存活监控，并发出 ready 事件。
   */
  private _attachBackend(connection: BackendConnection): void {
    this.attachedBackend = connection
    this.restarts = 0
    console.log(`[supervisor] 已连接独立 Python 后端，端口 ${connection.port}`)
    console.log(renderBackendEntryBox(connection, '状态：已连接正在运行的后端'))
    this._spawnAdapter()
    this.emit('ready', connection.port, connection.token)
    this._startAttachedMonitor()
  }

  /**
   * 启动外部后端的周期健康检查。
   *
   * @returns 无返回值；重复启动前会先清除旧定时器。
   * @sideEffects 创建不可阻止进程退出的轮询定时器。
   */
  private _startAttachedMonitor(): void {
    this._clearAttachedMonitor()
    this.attachedMonitor = setInterval(() => {
      void this._checkAttachedBackend()
    }, PythonSupervisor.ATTACHED_PROBE_INTERVAL)
    this.attachedMonitor.unref?.()
  }

  /**
   * 检查当前外部后端，失联时切换到重新接管或本地启动流程。
   *
   * @returns 完成一次健康检查后的 Promise。
   * @sideEffects 可能清理外部连接、停止适配器、发出 exit 并触发下一次连接尝试。
   */
  private async _checkAttachedBackend(): Promise<void> {
    const connection = this.attachedBackend
    if (this.stopping || connection === null) return
    if (await this._probeBackend(connection)) return
    if (this.attachedBackend !== connection) return
    this.attachedBackend = null
    this._clearAttachedMonitor()
    this._killAdapter()
    console.warn('[supervisor] 独立 Python 后端已断开，将重新连接或拉起')
    this.emit('exit', null)
    void this._connectOrSpawn()
  }

  /**
   * 清除外部后端存活监控定时器。
   *
   * @returns 无返回值；没有定时器时安全返回。
   * @sideEffects 停止周期健康检查并清空引用。
   */
  private _clearAttachedMonitor(): void {
    if (this.attachedMonitor === null) return
    clearInterval(this.attachedMonitor)
    this.attachedMonitor = null
  }

  /**
   * 终止主 Python 子进程，并在 Windows 上连同子进程树一起结束。
   *
   * @returns 无返回值；进程不存在或已退出时安全返回。
   * @sideEffects 调用 taskkill 或 ChildProcess.kill，并设置直接终止的超时兜底。
   */
  private _kill(): void {
    const child = this.child
    if (!child || child.exitCode !== null) return
    // Windows 没有真正的 SIGTERM；Node 会退化成 TerminateProcess，
    // 但 uvicorn 若已经 fork 出 reloader 子进程就会留孤儿。
    stopProcessTree(child)
  }

  /**
   * 启动一个本地 Python 后端子进程并注册输出、退出和错误处理器。
   *
   * @returns 无返回值；进程就绪后由 ``_onLine`` 发布 ready 事件。
   * @sideEffects 创建子进程、注入运行目录和 UTF-8 环境变量，并重置本次启动的信号状态。
   */
  private _spawn(): void {
    this.attachedBackend = null
    this._clearAttachedMonitor()
    const args = [
      'bot.py',
      '--data-dir', this.dataDir,
      '--config-path', this.configPath,
    ]
    this.stdoutBuf = ''
    this.backendPort = null
    this.backendToken = null
    this.backendReady = false
    this.readyEmitted = false

    const child = spawn(this.pythonExe, args, {
      cwd: this.cwd,
      env: {
        ...process.env,
        YUELI_DATA_DIR: this.dataDir,
        PYTHONUNBUFFERED: '1',
        // Windows 管道默认编码可能不是 UTF-8，显式设置避免中文日志解码错误。
        PYTHONIOENCODING: 'utf-8',
        // stdout 通过管道转发到 Electron 控制台，isatty() 为 false；显式保留彩色
        // 输出，避免日志转发后丢失可读性。
        YUELI_FORCE_COLOR: '1',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    this.child = child

    // spawn 失败通过异步 error 事件报告；必须监听，否则 Node 会将错误视为未处理
    // 事件并终止 Electron 主进程。
    child.on('error', (err) => {
      console.error('[supervisor] 无法拉起 Python 后端：', err.message)
      this.emit('failed', err)
    })

    child.stdout?.on('data', (chunk: Buffer) => this._onStdout(chunk))
    child.stderr?.on('data', (chunk: Buffer) => process.stderr.write(chunk))

    child.on('exit', (code, signal) => {
      const isCurrent = this.child === child
      if (isCurrent) {
        this.child = null
        this._killAdapter()
      }
      // stop() 主动关闭进程不属于异常退出。
      const details = formatProcessExitDetails(code, signal)
      if (this.stopping) console.info(`[supervisor] Python 后端已退出（${details}）`)
      else console.warn(`[supervisor] Python 后端退出（${details}）`)
      if (!isCurrent) return
      this.emit('exit', code)
      if (!this.stopping) this._scheduleRestart()
    })
  }

  /**
   * 将 Python stdout 按换行拆分并交给就绪信号解析器。
   *
   * @param chunk 子进程 stdout 的任意字节块，可能包含半行或多行文本。
   * @returns 无返回值；未完成的行保留在内部缓冲区。
   * @sideEffects 更新 stdout 缓冲区、触发 ready 状态处理并转发普通日志。
   */
  private _onStdout(chunk: Buffer): void {
    this.stdoutBuf += chunk.toString('utf8')
    let nl: number
    while ((nl = this.stdoutBuf.indexOf('\n')) !== -1) {
      const line = this.stdoutBuf.slice(0, nl).trim()
      this.stdoutBuf = this.stdoutBuf.slice(nl + 1)
      this._onLine(line)
    }
    // 无换行输出不能无限占用内存，超过上限后丢弃未成行内容。
    if (this.stdoutBuf.length > 64 * 1024) this.stdoutBuf = ''
  }

  /**
   * 解析单行后端公告并转发普通日志。
   *
   * @param line 去除换行后的 stdout 文本。
   * @returns 无返回值；端口、令牌和就绪标记分别更新对应状态。
   * @sideEffects 可能发布 ready 事件或写入 Electron stdout。
   */
  private _onLine(line: string): void {
    if (line.startsWith('YUELI_PORT=')) {
      const port = Number.parseInt(line.slice('YUELI_PORT='.length), 10)
      if (!Number.isInteger(port) || port <= 0 || port > 65535) return
      this.backendPort = port
      this._emitReadyIfComplete()
      return
    }
    if (line.startsWith('YUELI_TOKEN=')) {
      const token = line.slice('YUELI_TOKEN='.length)
      if (!/^[0-9a-f]{64}$/.test(token)) return
      this.backendToken = token
      this._emitReadyIfComplete()
      return
    }
    if (line === 'YUELI_READY=1') {
      this.backendReady = true
      this._emitReadyIfComplete()
      return
    }
    // 非协议行属于后端诊断输出，保留并转发到 Electron 控制台，便于定位启动阶段问题。
    if (line) process.stdout.write(`${line}\n`)
  }

  /**
   * 在端口、令牌和后端就绪信号全部收到后向下游发布一次 ``ready`` 事件。
   *
   * @returns {void} 条件未满足、事件已发布或发布成功后均无返回值。
   * @remarks 方法只允许发布一次 ready，并在稳定运行计时结束后清零连续重启计数。
   */
  private _emitReadyIfComplete(): void {
    if (
      this.readyEmitted
      || this.backendPort === null
      || this.backendToken === null
      || !this.backendReady
    ) return
    const port = this.backendPort
    const token = this.backendToken
    this.readyEmitted = true
    console.log(`[supervisor] Python 后端就绪，端口 ${port}`)
    // 稳定运行后才清零重启计数，避免短暂启动后立即崩溃耗尽退避次数。
    setTimeout(() => {
      if (this.alive) this.restarts = 0
    }, PythonSupervisor.STABLE_AFTER).unref?.()
    this._spawnAdapter()
    this.emit('ready', port, token)
  }

  /**
   * 按指数退避安排下一次后端启动。
   *
   * @returns 无返回值；达到重启上限时停止监护并发出错误日志。
   * @sideEffects 增加重启计数并创建可取消的延迟任务。
   */
  private _scheduleRestart(): void {
    if (this.restarts >= PythonSupervisor.MAX_RESTARTS) {
      console.error('[supervisor] Python 后端连续重启超过上限，停止监护')
      return
    }
    const delay = PythonSupervisor.BASE_BACKOFF * 2 ** this.restarts
    this.restarts++
    console.log(`[supervisor] 将在 ${delay}ms 后重启 Python 后端（第 ${this.restarts} 次）`)
    setTimeout(() => {
      if (!this.stopping) void this._connectOrSpawn()
    }, delay).unref?.()
  }

  /**
   * 在主体后端完成就绪后启动可选 QQ 适配器。
   *
   * @returns 无返回值；未配置适配器路径时直接返回。
   * @sideEffects 清理旧适配器、创建新 Python 进程并转发其标准输出和错误；适配器
   * 故障通过 ``adapterFailed`` 事件通知主进程，不参与主体重启计数。
   */
  private _spawnAdapter(): void {
    if (!this.napcatConfigPath) return
    this._killAdapter()
    this.adapterStdoutBuf = ''
    this.adapterStderrBuf = ''

    const adapter = spawn(this.pythonExe, [
      '-m', 'src.platforms.onebot11',
      '--adapter', 'yueli-snowluma-adapter',
      '--runtime-path', join(this.dataDir, 'runtime', 'backend.json'),
    ], {
      cwd: this.cwd,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
        PYTHONIOENCODING: 'utf-8',
        YUELI_FORCE_COLOR: '1',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    this.adapter = adapter
    let adapterSpawnError: Error | null = null

    // 先缓存 spawn 错误，由 close 统一合并退出状态后发出一次故障通知。
    adapter.on('error', (err) => {
      if (this.adapter !== adapter) return
      adapterSpawnError = err
    })
    adapter.stdout?.on('data', (chunk: Buffer) => this._onAdapterStdout(chunk))
    adapter.stderr?.on('data', (chunk: Buffer) => this._onAdapterStderr(chunk))
    // 使用 close 而不是 exit：spawn 失败时 Node 可能只发 error/close。
    adapter.on('close', (code, signal) => {
      if (this.adapter !== adapter) return
      this._flushAdapterOutput()
      this.adapter = null
      if (this.stopping || (code === 0 && signal === null)) {
        console.info(`[supervisor] QQ 适配器已退出（${formatProcessExitDetails(code, signal)}）`)
        return
      }
      // 无退出码时保留原始 spawn 错误，避免将启动失败误报为普通退出。
      const failure = adapterSpawnError
        ? new Error(`QQ 适配器拉起失败：${adapterSpawnError.message}`)
        : new Error(`QQ 适配器异常退出（${formatProcessExitDetails(code, signal)}）`)
      console.error(`[supervisor] ${failure.message}`)
      this.emit('adapterFailed', failure)
    })
  }

  /**
   * 终止当前 QQ 适配器进程树。
   *
   * @returns 无返回值；适配器不存在或已经退出时安全返回。
   * @sideEffects 在 Windows 使用 taskkill 终止进程树，其他平台发送 SIGTERM。
   */
  private _killAdapter(): void {
    const adapter = this.adapter
    if (!adapter || adapter.exitCode !== null) {
      this.adapter = null
      return
    }
    stopProcessTree(adapter)
  }

  /**
   * 缓冲 QQ 适配器标准输出并按行转发。
   *
   * @param chunk 适配器 stdout 的任意字节块。
   * @returns 无返回值。
   * @sideEffects 更新 stdout 缓冲并将完整行写入 Electron stdout。
   */
  private _onAdapterStdout(chunk: Buffer): void {
    this.adapterStdoutBuf += chunk.toString('utf8')
    this._writeAdapterLines(false, false)
  }

  /**
   * 缓冲 QQ 适配器标准错误并按行转发。
   *
   * @param chunk 适配器 stderr 的任意字节块。
   * @returns 无返回值。
   * @sideEffects 更新 stderr 缓冲并将完整行写入 Electron stderr。
   */
  private _onAdapterStderr(chunk: Buffer): void {
    this.adapterStderrBuf += chunk.toString('utf8')
    this._writeAdapterLines(true, false)
  }

  /**
   * 从适配器指定输出缓冲区提取完整行，并处理关闭时的末尾残片。
   *
   * @param isError 为 ``true`` 时处理 stderr，否则处理 stdout。
   * @param flush 为 ``true`` 时将末尾无换行文本也作为一行写出。
   * @returns 无返回值；超过 64 KiB 的无换行缓冲会被清空。
   * @sideEffects 更新对应缓冲区并向 Electron 输出带适配器前缀的文本。
   */
  private _writeAdapterLines(isError: boolean, flush: boolean): void {
    const buffer = isError ? this.adapterStderrBuf : this.adapterStdoutBuf
    let remaining = buffer
    let nl: number
    while ((nl = remaining.indexOf('\n')) !== -1) {
      this._writeAdapterLine(remaining.slice(0, nl), isError)
      remaining = remaining.slice(nl + 1)
    }
    if (flush && remaining) {
      this._writeAdapterLine(remaining, isError)
      remaining = ''
    }
    if (isError) this.adapterStderrBuf = remaining
    else this.adapterStdoutBuf = remaining
    if (remaining.length > 64 * 1024) {
      if (isError) this.adapterStderrBuf = ''
      else this.adapterStdoutBuf = ''
    }
  }

  /**
   * 将一行适配器输出写入 Electron stdout 或 stderr。
   *
   * @param line 待写出的文本行。
   * @param isError 是否写入 stderr。
   * @returns 无返回值；空白行不输出。
   * @sideEffects 写入当前进程的标准输出流。
   */
  private _writeAdapterLine(line: string, isError: boolean): void {
    const text = line.trimEnd()
    if (!text) return
    const output = isError ? process.stderr : process.stdout
    output.write(`[napcat] ${text}\n`)
  }

  /**
   * 在适配器关闭时强制输出 stdout/stderr 缓冲区中的最后残片。
   *
   * @returns 无返回值。
   * @sideEffects 清空可输出的适配器缓冲并写入当前进程输出流。
   */
  private _flushAdapterOutput(): void {
    this._writeAdapterLines(false, true)
    this._writeAdapterLines(true, true)
  }
}
