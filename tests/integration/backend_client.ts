/**
 * BackendLink 与 PythonClient 的真实跨进程集成测试。
 *
 * 不需要 Electron。直接用 tsx 运行：
 *   npx tsx tests/integration/backend_client.ts
 *
 * 验收条件：
 *   1. 按真实入口启动 Python 后端（bot.py --no-shell），BackendLink 连上它
 *   2. PythonClient 用子协议 token 握手成功
 *   3. 错 token 握手被拒，client 停止重连（不无限循环）
 *   4. HTTP /health 可达，/diary /observability 带正确 token 可达
 *   5. 后端消失 → BackendLink 发 lost；后端重新起来 → 再次发 ready
 *   6. link.stop() 只断开连接，不终止后端进程
 *
 * 第 5 条与入口反转前的语义不同：重启后端不再是 Electron 的职责，它只负责重新连上。
 */

import { spawn } from 'node:child_process'
import type { ChildProcess } from 'node:child_process'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import fs from 'node:fs'
import os from 'node:os'

import { BackendLink } from '../../electron/main/python/backendLink.ts'
import { PythonClient } from '../../electron/main/python/client.ts'

const PROJECT_ROOT = join(fileURLToPath(import.meta.url), '../../..')
// Python 后端必须从仓库根启动，才能同时解析 bot.py 和 src/ 包。
const PYTHON_CWD = PROJECT_ROOT
const PYTHON_EXE = process.env.YUELI_PYTHON_EXE ?? 'python'

// ── EventSink 替身：记录发送事件，不依赖真实 BrowserWindow ────────────────

interface ReceivedEvent {
  channel: string
  payload: unknown
}

/**
 * 创建记录主进程事件的最小 EventSink 替身。
 *
 * @returns {{send: function, isAlive: function, events: ReceivedEvent[]}} 始终可用且保存已发送事件的替身。
 * @sideEffects 返回对象的 send 方法会把事件追加到 events 数组。
 */
function makeSink(): {
  send(channel: string, payload: unknown): void
  isAlive(): boolean
  events: ReceivedEvent[]
} {
  const events: ReceivedEvent[] = []
  return {
    send: (channel, payload) => { events.push({ channel, payload }) },
    isAlive: () => true,
    events,
  }
}

// ── 测试辅助函数 ─────────────────────────────────────────────────────────────

/**
 * 按真实入口启动一个 Python 后端子进程。
 *
 * @param {string} dataDir 后端使用的数据目录。
 * @returns {ChildProcess} 已启动的子进程；调用方负责终止。
 * @sideEffects 创建 Python 子进程，其 stdout 转发到当前进程。
 */
function startBackend(dataDir: string): ChildProcess {
  // --no-shell：本测试只验证连接层，不需要真的弹出一个桌宠窗口。
  const child = spawn(PYTHON_EXE, [
    'bot.py',
    '--data-dir', dataDir,
    '--config-path', join(PROJECT_ROOT, 'config'),
    '--no-shell',
  ], {
    cwd: PYTHON_CWD,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  child.stdout?.on('data', (chunk: Buffer) => {
    const text = chunk.toString('utf8').trimEnd()
    if (text) console.log(`[backend] ${text}`)
  })
  return child
}

/**
 * 等待 BackendLink 发出 ready 并返回连接坐标。
 *
 * @param {BackendLink} link 后端连接管理器。
 * @param {number} [timeout=40000] 最大等待毫秒数。
 * @returns {Promise<{port: number, token: string}>} 连接坐标。
 * @throws {Error} 超时仍未连上时抛出。
 */
function waitReady(
  link: BackendLink,
  timeout = 40_000,
): Promise<{ port: number; token: string }> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('等待 ready 超时')), timeout)
    link.once('ready', (port, token) => {
      clearTimeout(timer)
      resolve({ port, token })
    })
  })
}

/**
 * 等待 BackendLink 发出 lost。
 *
 * @param {BackendLink} link 后端连接管理器。
 * @param {number} [timeout=20000] 最大等待毫秒数。
 * @returns {Promise<void>} 事件到达后完成。
 * @throws {Error} 超时仍未收到时抛出。
 */
function waitLost(link: BackendLink, timeout = 20_000): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('等待 lost 超时')), timeout)
    link.once('lost', () => {
      clearTimeout(timer)
      resolve()
    })
  })
}

/** 只允许访问本机回环地址的健康检查端点；端口来自受测后端的公告。 */
function loopbackHealthUrl(port: number): URL {
  const parsed = new URL(`http://127.0.0.1:${port}/health`)
  if (parsed.hostname !== '127.0.0.1') {
    throw new Error(`健康检查目标必须是回环地址，收到 ${parsed.hostname}`)
  }
  return parsed
}

/**
 * 轮询 /health 直到返回成功或超时。
 *
 * @param {number} port 后端端口。
 * @param {string} token 认证令牌。
 * @returns {Promise<void>} 就绪后完成。
 * @throws {Error} 15 秒内仍不可用时抛出。
 */
async function waitHealthy(port: number, token: string): Promise<void> {
  const deadline = Date.now() + 15_000
  while (Date.now() < deadline) {
    try {
      const response = await fetch(loopbackHealthUrl(port), {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(1_000),
      })
      if (response.ok) return
    } catch { /* 后端尚未就绪，继续按固定间隔轮询。 */ }
    await sleep(300)
  }
  throw new Error('/health 在 15000ms 内未就绪')
}

/**
 * 终止子进程并等待它真正退出。
 *
 * @param {ChildProcess} child 目标子进程。
 * @returns {Promise<void>} 进程退出后完成。
 * @sideEffects 向子进程发送终止信号。
 */
async function killBackend(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null) return
  child.kill('SIGKILL')
  await new Promise((resolve) => child.once('exit', resolve))
}

// ── 断言统计 ─────────────────────────────────────────────────────────────────

let passed = 0
let failed = 0

/**
 * 记录一条集成测试断言结果。
 *
 * @param {boolean} condition 断言条件。
 * @param {string} label 失败或成功时显示的说明文本。
 * @returns {void} 不返回值。
 * @sideEffects 更新 passed 或 failed 计数，并向标准输出写入结果。
 */
function assert(condition: boolean, label: string): void {
  if (condition) {
    console.log(`  ✓ ${label}`)
    passed++
  } else {
    console.error(`  ✗ ${label}`)
    failed++
  }
}

// ── 集成场景 ─────────────────────────────────────────────────────────────────

/**
 * 执行连接、握手、HTTP 端点、失联重连与断开清理的完整集成场景。
 *
 * @returns {Promise<void>} 场景执行完成后的 Promise。
 * @throws {Error} 子进程启动失败、连接超时、断言失败或清理失败时抛出。
 * @sideEffects 启动本地 Python 子进程、创建临时目录、发送 HTTP/WebSocket 请求并输出测试结果。
 */
async function main(): Promise<void> {
  const tmpDir = fs.mkdtempSync(join(os.tmpdir(), 'yueli-itest-'))
  console.log(`[test] tmpDir: ${tmpDir}`)

  // 配置读取必须走项目现有目录；数据库改写到临时目录，避免测试污染运行时数据。
  const link = new BackendLink({ dataDir: tmpDir })
  let backend: ChildProcess | null = null

  try {
    // ── 1. 后端起来，连接层连上 ───────────────────────────────────────────────
    console.log('\n[1] 启动 Python 后端并连接…')
    backend = startBackend(tmpDir)
    const readyOnce = waitReady(link)
    link.start()
    const { port, token } = await readyOnce
    assert(typeof port === 'number' && port > 0, `端口宣告：${port}`)
    assert(/^[0-9a-f]{64}$/.test(token), 'Python token 公告合法')
    assert(link.alive, 'link.alive === true')

    await waitHealthy(port, token)
    console.log(`  /health 就绪（端口 ${port}）`)

    // ── 2. 正确 token 握手 ────────────────────────────────────────────────────
    console.log('\n[2] 正确 token WS 握手…')
    const sink = makeSink()
    const client = new PythonClient(port, token, sink)
    client.connect()
    await sleep(3_000)
    assert(client.connected, 'WS 握手成功 (connected = true)')

    // ── 3. HTTP 端点 ──────────────────────────────────────────────────────────
    console.log('\n[3] HTTP 端点…')
    const health = await client.health()
    assert(health, '/health 返回 ok')

    const diary = await client.diary() as Record<string, unknown>
    assert(typeof diary === 'object' && diary !== null, '/diary 返回对象')
    assert(!('_stub' in diary) || Object.keys(diary).length > 1, '/diary 有实际数据')

    const obs = await client.observability() as Record<string, unknown>
    assert(typeof obs === 'object' && obs !== null, '/observability 返回对象')

    // ── 4. 错 token 被拒，不无限重连 ─────────────────────────────────────────
    console.log('\n[4] 错 token WS 应被拒绝…')
    const badSink = makeSink()
    const badClient = new PythonClient(port, 'WRONG-TOKEN-NEVER-RIGHT', badSink)
    badClient.connect()
    await sleep(4_000)
    // 握手被拒后 authRejected = true，不再重连，connected 应为 false
    assert(!badClient.connected, '错 token 握手被拒（connected = false）')
    badClient.stop()

    // ── 5. 后端消失与重新出现 ────────────────────────────────────────────────
    console.log('\n[5] 杀掉后端并验证失联与重连…')
    client.stop()
    const lost = waitLost(link)
    await killBackend(backend)
    await lost
    assert(!link.alive, '失联后 link.alive === false')

    const readyAgain = waitReady(link)
    backend = startBackend(tmpDir)
    const { port: newPort, token: newToken } = await readyAgain
    assert(newToken !== token, '后端重启后由 Python 轮换 token')
    await waitHealthy(newPort, newToken)

    const newSink = makeSink()
    const newClient = new PythonClient(newPort, newToken, newSink)
    newClient.connect()
    await sleep(3_000)
    assert(newClient.connected, '重启后新 client 握手成功')
    newClient.stop()

    // ── 6. stop() 只断连接，不动后端 ─────────────────────────────────────────
    console.log('\n[6] link.stop() 后后端仍在运行…')
    link.stop()
    await sleep(500)
    assert(!link.alive, 'stop() 后 alive === false')
    assert(backend.exitCode === null, 'stop() 没有终止后端进程')

  } catch (err) {
    console.error('\n[test] 异常：', err)
    failed++
  } finally {
    link.stop()
    if (backend) await killBackend(backend)
    try { fs.rmSync(tmpDir, { recursive: true }) } catch { /* ignore */ }
  }

  console.log(`\n${'─'.repeat(50)}`)
  console.log(`结果：${passed} 通过 / ${failed} 失败`)
  if (failed > 0) process.exit(1)
}

main().catch((err) => { console.error(err); process.exit(1) })
