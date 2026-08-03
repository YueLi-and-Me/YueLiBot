/**
 * Python 后端进程监护。
 *
 * 职责：
 *  · 拉起 `python -m yueli` 子进程
 *  · 解析 stdout 里的 YUELI_PORT=<n>，通知 client.ts 建立连接
 *  · stderr 转发到 console（structlog 的输出）
 *  · 异常退出时按退避策略重启
 *  · 关停时确保子进程树真的死掉
 *
 * ★ 刻意不 import electron。
 *   一是为了能在无头 Node 里做真实联调测试（拉起真的 Python 再断言），
 *   二是 app.getAppPath() 在打包后指向 app.asar，而 python/ 不在 asar 里 ——
 *   路径该由调用方决定，监护器不该猜。
 */

import { ChildProcess, spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { EventEmitter } from 'node:events'

export interface SupervisorEvents {
  ready: [port: number]
  exit: [code: number | null]
  /** 拉不起来（python 不存在、权限不足）。不发这个事件的话 error 会成为未捕获异常。 */
  failed: [error: Error]
}

export interface SupervisorOptions {
  /** memory.db 所在目录。 */
  dataDir: string
  /** config.toml 的完整路径（含文件名）。 */
  configPath: string
  /** python 包的工作目录（含 yueli/ 的那一层）。 */
  cwd: string
  pythonExe?: string
}

export class PythonSupervisor extends EventEmitter<SupervisorEvents> {
  private child: ChildProcess | null = null
  private restarts = 0
  private stopping = false
  /** stdout 行缓冲。网络/管道分包与行边界无关，不缓冲会漏掉被切断的 YUELI_PORT=。 */
  private stdoutBuf = ''
  private portAnnounced = false
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

  constructor(opts: SupervisorOptions) {
    super()
    this.token = randomUUID()
    this.dataDir = opts.dataDir
    this.configPath = opts.configPath
    this.cwd = opts.cwd
    this.pythonExe = opts.pythonExe ?? 'python'
  }

  start(): void {
    this.stopping = false
    this._spawn()
  }

  stop(): void {
    this.stopping = true
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
    // 用 taskkill /T 杀整棵树才干净
    if (process.platform === 'win32' && child.pid) {
      spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' })
        .on('error', () => child.kill())
    } else {
      child.kill('SIGTERM')
    }
  }

  private _spawn(): void {
    const args = [
      '-m', 'yueli',
      '--data-dir', this.dataDir,
      '--config-path', this.configPath,
      '--token', this.token,
    ]
    this.stdoutBuf = ''
    this.portAnnounced = false

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
      if (this.child === child) this.child = null
      console.warn(`[supervisor] Python 后端退出（code=${code} signal=${signal}）`)
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
    if (!this.portAnnounced && line.startsWith('YUELI_PORT=')) {
      const port = Number.parseInt(line.slice('YUELI_PORT='.length), 10)
      if (!Number.isInteger(port) || port <= 0 || port > 65535) return
      this.portAnnounced = true
      console.log(`[supervisor] Python 后端就绪，端口 ${port}`)
      // 稳定跑够一段时间才认为这次拉起成功，避免「起来就崩」把退避耗尽
      setTimeout(() => {
        if (this.alive) this.restarts = 0
      }, PythonSupervisor.STABLE_AFTER).unref?.()
      this.emit('ready', port)
      return
    }
    // ★ 其余每一行都是 Python 侧真的想给人看的输出——structlog 日志、
    //   trace_console 的 rich 面板。以前这里只找端口号，其它行直接吞掉，
    //   Electron 控制台里等于永远看不到 Python 那边发生了什么。
    if (line) process.stdout.write(`${line}\n`)
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
}
