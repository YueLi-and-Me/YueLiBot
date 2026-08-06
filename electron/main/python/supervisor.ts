/**
 * Python 后端进程监护。
 *
 * 职责：
 *  · 拉起 `python bot.py` 子进程（仓库根的入口，业务在 src/ 下）
 *  · 解析 stdout 里的 YUELI_PORT=<n>，通知 client.ts 建立连接
 *  · stderr 转发到 console（structlog 的输出）
 *  · 异常退出时按退避策略重启
 *  · 关停时确保子进程树真的死掉
 *
 * ★ 刻意不 import electron。
 *   一是为了能在无头 Node 里做真实联调测试（拉起真的 Python 再断言），
 *   二是 app.getAppPath() 在打包后指向 app.asar，而 Python 侧（bot.py 与 src/）
 *   不在 asar 里 —— 路径该由调用方决定，监护器不该猜。
 */

import { spawn } from 'node:child_process'
import type { ChildProcess } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { EventEmitter } from 'node:events'

export interface SupervisorEvents {
  ready: [port: number]
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
  /** stdout 行缓冲。网络/管道分包与行边界无关，不缓冲会漏掉半截公告。 */
  private stdoutBuf = ''
  private adapterStdoutBuf = ''
  private adapterStderrBuf = ''
  /** 后端启动公告集合；未来加入 token 时继续在这里追加条件。 */
  private backendPort: number | null = null
  private backendReady = false
  private readyEmitted = false
  /** 最大重启次数。超过后等用户重启 Electron。 */
  private static readonly MAX_RESTARTS = 5
  /** 退避基准（毫秒）。 */
  private static readonly BASE_BACKOFF = 2_000
  /** 稳定运行超过这个时长就认为上次重启成功，清零计数。 */
  private static readonly STABLE_AFTER = 60_000

  readonly token: string
  private readonly dataDir: string
  private readonly configPath: string
  private readonly cwd: string
  private readonly pythonExe: string
  private readonly napcatConfigPath: string | null

  constructor(opts: SupervisorOptions) {
    super()
    this.token = randomUUID()
    this.dataDir = opts.dataDir
    this.configPath = opts.configPath
    this.cwd = opts.cwd
    this.pythonExe = opts.pythonExe ?? 'python'
    this.napcatConfigPath = opts.napcatConfigPath ?? null
  }

  start(): void {
    this.stopping = false
    this._spawn()
  }

  stop(): void {
    this.stopping = true
    this._killAdapter()
    this._kill()
  }

  /** 当前是否有活着的子进程。供自检与集成测试断言。 */
  get alive(): boolean {
    return !!this.child && this.child.exitCode === null && !this.child.killed
  }

  private _kill(): void {
    const child = this.child
    if (!child || child.exitCode !== null) return
    // Windows 没有真正的 SIGTERM；Node 会退化成 TerminateProcess，
    // 但 uvicorn 若已经 fork 出 reloader 子进程就会留孤儿。
    // 优先用 taskkill /T 杀整棵树；Windows 的 taskkill 偶尔会迟迟不返回，
    // 不能让已知的 Python 父进程一直活着阻塞 Electron 退出，所以保留直接终止兜底。
    if (process.platform === 'win32' && child.pid) {
      const terminateDirectly = () => {
        if (child.exitCode === null) child.kill()
      }
      const fallback = setTimeout(terminateDirectly, 2_000)
      const taskkill = spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], {
        stdio: 'ignore',
      })
      const finish = () => {
        clearTimeout(fallback)
        terminateDirectly()
      }
      taskkill.on('error', finish)
      taskkill.on('close', finish)
    } else {
      child.kill('SIGTERM')
    }
  }

  private _spawn(): void {
    const args = [
      'bot.py',
      '--data-dir', this.dataDir,
      '--config-path', this.configPath,
      '--token', this.token,
    ]
    this.stdoutBuf = ''
    this.backendPort = null
    this.backendReady = false
    this.readyEmitted = false

    const child = spawn(this.pythonExe, args, {
      cwd: this.cwd,
      env: {
        ...process.env,
        YUELI_TOKEN: this.token,
        YUELI_DATA_DIR: this.dataDir,
        PYTHONUNBUFFERED: '1',
        // Windows 控制台默认 GBK，structlog 的中文会变成乱码甚至
        // 在 print 时抛 UnicodeEncodeError 把进程带崩
        PYTHONIOENCODING: 'utf-8',
        // stdout 是管道，Python 那边 sys.stdout.isatty() 永远是 False——
        // 但这个管道最终确实会被转发进一个真终端（见下面 _onLine），
        // 所以显式告诉 Python 侧「按彩色渲染」，不要被 isatty() 的假阴性坑了。
        YUELI_FORCE_COLOR: '1',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    this.child = child

    // ★ spawn 失败（ENOENT：python 不在 PATH）是**异步**发 error 事件，不是抛异常。
    //   不挂这个监听器，未捕获的 error 会直接崩掉 Electron 主进程 ——
    //   表现为「桌宠根本没出现」，而不是「后端没起来」
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
      // 与适配器那条统一：signal 为空就不打这个字段，不编一个 null/unknown 出来；
      // 主动关停不是故障，不占用 warn。
      const details = formatProcessExitDetails(code, signal)
      if (this.stopping) console.info(`[supervisor] Python 后端已退出（${details}）`)
      else console.warn(`[supervisor] Python 后端退出（${details}）`)
      if (!isCurrent) return
      this.emit('exit', code)
      if (!this.stopping) this._scheduleRestart()
    })
  }

  /** 按行解析 stdout，跨 chunk 的半截行留在缓冲里。 */
  private _onStdout(chunk: Buffer): void {
    this.stdoutBuf += chunk.toString('utf8')
    let nl: number
    while ((nl = this.stdoutBuf.indexOf('\n')) !== -1) {
      const line = this.stdoutBuf.slice(0, nl).trim()
      this.stdoutBuf = this.stdoutBuf.slice(nl + 1)
      this._onLine(line)
    }
    // 防止对端一直不换行时缓冲无限增长
    if (this.stdoutBuf.length > 64 * 1024) this.stdoutBuf = ''
  }

  private _onLine(line: string): void {
    if (line.startsWith('YUELI_PORT=')) {
      const port = Number.parseInt(line.slice('YUELI_PORT='.length), 10)
      if (!Number.isInteger(port) || port <= 0 || port > 65535) return
      this.backendPort = port
      this._emitReadyIfComplete()
      return
    }
    if (line === 'YUELI_READY=1') {
      this.backendReady = true
      this._emitReadyIfComplete()
      return
    }
    // ★ 其余每一行都是 Python 侧真的想给人看的输出——structlog 日志、
    //   trace_console 的 rich 面板。以前这里只找端口号，其它行直接吞掉，
    //   Electron 控制台里等于永远看不到 Python 那边发生了什么。
    if (line) process.stdout.write(`${line}\n`)
  }

  /** 只有端口和 FastAPI 生命周期都公告后，才允许下游建立连接。 */
  private _emitReadyIfComplete(): void {
    if (this.readyEmitted || this.backendPort === null || !this.backendReady) return
    const port = this.backendPort
    this.readyEmitted = true
    console.log(`[supervisor] Python 后端就绪，端口 ${port}`)
    // 稳定跑够一段时间才认为这次拉起成功，避免「起来就崩」把退避耗尽
    setTimeout(() => {
      if (this.alive) this.restarts = 0
    }, PythonSupervisor.STABLE_AFTER).unref?.()
    this._spawnAdapter(port)
    this.emit('ready', port)
  }

  private _scheduleRestart(): void {
    if (this.restarts >= PythonSupervisor.MAX_RESTARTS) {
      console.error('[supervisor] Python 后端连续重启超过上限，停止监护')
      return
    }
    const delay = PythonSupervisor.BASE_BACKOFF * 2 ** this.restarts
    this.restarts++
    console.log(`[supervisor] 将在 ${delay}ms 后重启 Python 后端（第 ${this.restarts} 次）`)
    setTimeout(() => {
      if (!this.stopping) this._spawn()
    }, delay).unref?.()
  }

  /**
   * 后端端口和生命周期都完成后才会宣告 ready，此时再把适配器接上，避免给它
   * 传一个尚未可用的端口。适配器不参与主体重启退避；它自己的连接失败策略由
   * runner 执行，配置或协议端错误则直接通过托盘告诉用户。
   */
  private _spawnAdapter(port: number): void {
    if (!this.napcatConfigPath) return
    this._killAdapter()
    this.adapterStdoutBuf = ''
    this.adapterStderrBuf = ''

    const adapter = spawn(this.pythonExe, [
      '-m', 'src.adapters.napcat',
      '--config-path', this.napcatConfigPath,
      '--backend-port', String(port),
      '--token', this.token,
    ], {
      cwd: this.cwd,
      env: {
        ...process.env,
        YUELI_TOKEN: this.token,
        PYTHONUNBUFFERED: '1',
        PYTHONIOENCODING: 'utf-8',
        YUELI_FORCE_COLOR: '1',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    this.adapter = adapter
    let adapterSpawnError: Error | null = null

    // 只记录不输出：退出诊断统一由下面那一个处理器出，避免同一件事打两行。
    adapter.on('error', (err) => {
      if (this.adapter !== adapter) return
      adapterSpawnError = err
    })
    adapter.stdout?.on('data', (chunk: Buffer) => this._onAdapterStdout(chunk))
    adapter.stderr?.on('data', (chunk: Buffer) => this._onAdapterStderr(chunk))
    // ★ 挂 close 而不是 exit。spawn 本身失败（可执行文件不存在、权限不足）时 Node
    //   发的是 error + close，**不会发 exit** —— 挂 exit 就等于让「适配器根本没拉起来」
    //   变成一条日志都没有的静默失败。close 两种情况都会到，且它在 stdio 关闭之后才发，
    //   正好是 _flushAdapterOutput() 想要的时机。
    adapter.on('close', (code, signal) => {
      if (this.adapter !== adapter) return
      this._flushAdapterOutput()
      this.adapter = null
      if (this.stopping || (code === 0 && signal === null)) {
        console.info(`[supervisor] QQ 适配器已退出（${formatProcessExitDetails(code, signal)}）`)
        return
      }
      // 拉起失败没有退出码可言，说「异常退出（无退出详情）」是在编造信息；分开措辞。
      const failure = adapterSpawnError
        ? new Error(`QQ 适配器拉起失败：${adapterSpawnError.message}`)
        : new Error(`QQ 适配器异常退出（${formatProcessExitDetails(code, signal)}）`)
      console.error(`[supervisor] ${failure.message}`)
      this.emit('adapterFailed', failure)
    })
  }

  /** 适配器只由其自己的退出事件结束，避免 stop() 触发一次无意义的托盘告警。 */
  private _killAdapter(): void {
    const adapter = this.adapter
    if (!adapter || adapter.exitCode !== null) {
      this.adapter = null
      return
    }
    if (process.platform === 'win32' && adapter.pid) {
      const terminateDirectly = () => {
        if (adapter.exitCode === null) adapter.kill()
      }
      const fallback = setTimeout(terminateDirectly, 2_000)
      const taskkill = spawn('taskkill', ['/pid', String(adapter.pid), '/T', '/F'], {
        stdio: 'ignore',
      })
      const finish = () => {
        clearTimeout(fallback)
        terminateDirectly()
      }
      taskkill.on('error', finish)
      taskkill.on('close', finish)
    } else {
      adapter.kill('SIGTERM')
    }
  }

  private _onAdapterStdout(chunk: Buffer): void {
    this.adapterStdoutBuf += chunk.toString('utf8')
    this._writeAdapterLines(false, false)
  }

  private _onAdapterStderr(chunk: Buffer): void {
    this.adapterStderrBuf += chunk.toString('utf8')
    this._writeAdapterLines(true, false)
  }

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

  private _writeAdapterLine(line: string, isError: boolean): void {
    const text = line.trimEnd()
    if (!text) return
    const output = isError ? process.stderr : process.stdout
    output.write(`[napcat] ${text}\n`)
  }

  private _flushAdapterOutput(): void {
    this._writeAdapterLines(false, true)
    this._writeAdapterLines(true, true)
  }
}
