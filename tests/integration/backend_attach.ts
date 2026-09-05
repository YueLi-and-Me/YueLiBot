/**
 * BackendLink 接管正在运行的 Python 后端的集成测试。
 *
 * 直接用 tsx 运行：
 *   npx tsx tests/integration/backend_attach.ts
 *
 * 本模块属于 Electron 与 Python 运行时的跨进程测试，先按真实入口启动一个 Python
 * 后端，再验证 BackendLink 能连上它、`stop()` 不会终止它、后端消失后按事件报告失联
 * 与不可用。测试依赖 bot.py、electron/main/python/backendLink.ts 以及本机可执行的
 * Python 环境，会在 data/integration-tests 下创建临时运行目录。
 *
 * 入口反转之后 Electron 侧不再拉起任何进程，因此本文件也不再验证「接管失败后自己
 * 拉一个」——那条行为已经删除，验收点相应换成「报告不可用」。
 */
import { execFile } from 'node:child_process'
import type { ChildProcess } from 'node:child_process'
import { mkdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { BackendLink } from '../../electron/main/python/backendLink.ts'


const PROJECT_ROOT = join(fileURLToPath(import.meta.url), '../../..')

/**
 * 等待后端输出完整的端口、令牌和就绪公告。
 *
 * @param {ChildProcess} child 已启动且 stdout 可读的 Python 子进程。
 * @returns {Promise<{port: number, token: string}>} 解析出的后端端口和访问令牌。
 * @throws {Error} 子进程在输出就绪信息前退出或超过 30 秒仍未就绪时抛出。
 * @sideEffects 持续读取 stdout，并创建一个最多等待 30 秒的定时器。
 */
function waitBackendReady(child: ChildProcess): Promise<{ port: number; token: string }> {
  return new Promise((resolve, reject) => {
    let buffer = ''
    let port: number | null = null
    let token: string | null = null
    let ready = false
    const timer = setTimeout(() => reject(new Error('后端启动超时')), 30_000)
    const complete = () => {
      if (port === null || token === null || !ready) return
      clearTimeout(timer)
      resolve({ port, token })
    }
    child.stdout?.on('data', (chunk: Buffer) => {
      buffer += chunk.toString('utf8')
      let newline: number
      while ((newline = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, newline).trim()
        buffer = buffer.slice(newline + 1)
        if (line.startsWith('YUELI_PORT=')) port = Number(line.slice(11))
        if (line.startsWith('YUELI_TOKEN=')) token = line.slice(12)
        if (line === 'YUELI_READY=1') ready = true
        complete()
      }
    })
    child.on('exit', (code) => reject(new Error(`后端提前退出：code=${code}`)))
  })
}

/**
 * 等待 BackendLink 发出指定事件。
 *
 * @param {BackendLink} link 待观察的后端连接管理器。
 * @param {'ready' | 'lost' | 'unavailable'} event 事件名。
 * @param {number} [timeout=8000] 最大等待时长，单位为毫秒，必须为正数。
 * @returns {Promise<void>} 事件到达后的完成 Promise。
 * @throws {Error} 超过 timeout 仍未收到事件时抛出。
 * @sideEffects 注册一次性监听器，并创建等待定时器。
 */
function waitLinkEvent(
  link: BackendLink,
  event: 'ready' | 'lost' | 'unavailable',
  timeout = 8_000,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`等待 ${event} 超时`)), timeout)
    link.once(event, () => {
      clearTimeout(timer)
      resolve()
    })
  })
}

/**
 * 执行接管、所有权保持与失联报告的完整集成流程。
 *
 * @returns {Promise<void>} 所有断言通过后的完成 Promise。
 * @throws {Error} 后端未就绪、端口或令牌不一致、进程所有权错误或 HTTP 检查失败时抛出。
 * @sideEffects 启动和停止 Python 子进程、创建临时数据目录并访问本地 HTTP 接口。
 */
async function main(): Promise<void> {
  const testRoot = join(PROJECT_ROOT, 'data', 'integration-tests')
  mkdirSync(testRoot, { recursive: true })
  // 固定子目录加运行前清空代替随机临时目录：mkdtemp 返回的路径属于外部值，
  // 不允许进入解释器参数表；固定路径由字面量 join 得到，且清空保证隔离。
  const dataDir = join(testRoot, 'backend-attach-run')
  rmSync(dataDir, { recursive: true, force: true })
  mkdirSync(dataDir, { recursive: true })
  // execFile 永不经由 shell 解释参数，动态路径只会作为独立参数传给固定的
  // python 解释器；解释器名保持字面量，不存在由变量决定启动哪个程序的路径。
  // --no-shell：本测试只验证连接层，不需要真的弹出一个桌宠窗口。
  const backendArgs = [
    'bot.py',
    '--data-dir', dataDir,
    '--config-path', join(PROJECT_ROOT, 'config'),
    '--no-shell',
  ]
  const backendOptions = {
    cwd: PROJECT_ROOT,
    env: {
      ...process.env,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1',
    },
    stdio: ['ignore', 'pipe', 'pipe'] as const,
  }
  const backend: ChildProcess = execFile('python', backendArgs, backendOptions)

  // 连接窗口收窄到 3 秒：后端已经在跑，连不上就是真连不上，不必等默认的 30 秒。
  const link = new BackendLink({ dataDir, attachTimeout: 3_000 })
  let attachedPort = 0
  let attachedToken = ''
  link.on('ready', (port, token) => {
    attachedPort = port
    attachedToken = token
  })
  try {
    const runtime = await waitBackendReady(backend)
    const attached = waitLinkEvent(link, 'ready')
    link.start()
    await attached

    if (attachedPort !== runtime.port) throw new Error('端口不一致')
    if (attachedToken !== runtime.token) throw new Error('token 不一致')

    link.stop()
    await new Promise((resolve) => setTimeout(resolve, 300))
    if (backend.exitCode !== null) throw new Error('link.stop() 杀掉了后端')

    const response = await fetch(`http://127.0.0.1:${runtime.port}/observability`, {
      headers: { Authorization: `Bearer ${runtime.token}` },
    })
    if (!response.ok) throw new Error(`后端在 Electron 退出后不可用：${response.status}`)

    const attachedAgain = waitLinkEvent(link, 'ready')
    link.start()
    await attachedAgain

    // 后端消失之后：先失联、再在连接窗口耗尽时报告不可用；全程不得创建新进程。
    const lost = waitLinkEvent(link, 'lost', 15_000)
    const unavailable = waitLinkEvent(link, 'unavailable', 20_000)
    backend.kill()
    await new Promise((resolve) => backend.once('exit', resolve))
    await lost
    await unavailable
    if (link.alive) throw new Error('后端已退出，link.alive 仍为 true')
    console.log('后端连接集成验收：通过（接管、所有权、失联与不可用报告）')
  } finally {
    link.stop()
    if (backend.exitCode === null) {
      backend.kill()
      await new Promise((resolve) => backend.once('exit', resolve))
    }
  }
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
