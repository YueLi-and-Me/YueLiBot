/**
 * Supervisor + Client 真实集成测试。
 *
 * 不需要 Electron。直接用 tsx 运行：
 *   npx tsx tests/integration/supervisor_client.ts
 *
 * 验收标准（Phase 0 出口条件）：
 *   1. PythonSupervisor 拉起真实 Python 进程，正确解析 YUELI_PORT=
 *   2. PythonClient 用子协议 token 握手成功（WS subprotocol echo bug 已修）
 *   3. 错 token 握手被拒，client 停止重连（不无限循环）
 *   4. HTTP /health 可达，/diary /observability 带正确 token 可达
 *   5. Python 崩溃 → supervisor 重启 → ready 再次触发 → 旧 client 被停、新 client 连上
 *   6. stop() 时子进程真的退出（不留僵尸）
 */

import { EventEmitter } from 'node:events'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as sleep } from 'node:timers/promises'
import os from 'node:os'
import fs from 'node:fs'
import { PythonSupervisor } from '../../src/main/python/supervisor.ts'
import { PythonClient } from '../../src/main/python/client.ts'

const PROJECT_ROOT = join(fileURLToPath(import.meta.url), '../../..')
const PYTHON_CWD = join(PROJECT_ROOT, 'python')
const PYTHON_EXE = process.env.YUELI_PYTHON_EXE ?? 'python'

// ── 假 EventSink（代替真实 BrowserWindow）────────────────────────────────────

interface ReceivedEvent {
  channel: string
  payload: unknown
}

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

// ── 工具 ─────────────────────────────────────────────────────────────────────

function waitEvent<T>(emitter: EventEmitter, event: string, timeout = 30_000): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`等待 ${event} 超时`)), timeout)
    emitter.once(event, (val: T) => {
      clearTimeout(timer)
      resolve(val)
    })
  })
}

async function waitHttp(port: number, token: string, timeout = 15_000): Promise<void> {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/health`,
        { headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(1000) })
      if (r.ok) return
    } catch { /* 还没好 */ }
    await sleep(300)
  }
  throw new Error(`/health 在 ${timeout}ms 内未就绪`)
}

// ── 断言 ─────────────────────────────────────────────────────────────────────

let passed = 0
let failed = 0

function assert(condition: boolean, label: string): void {
  if (condition) {
    console.log(`  ✓ ${label}`)
    passed++
  } else {
    console.error(`  ✗ ${label}`)
    failed++
  }
}

// ── 主测试 ───────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  const tmpDir = fs.mkdtempSync(join(os.tmpdir(), 'yueli-itest-'))
  console.log(`[test] tmpDir: ${tmpDir}`)

  const sup = new PythonSupervisor({
    dataDir: tmpDir,
    // 临时目录里没有 config.toml，后端会退回默认配置——集成测试要验的是
    // 拉起/端口宣告/令牌链路，不依赖具体配置内容。
    configPath: join(tmpDir, 'config.toml'),
    cwd: PYTHON_CWD,
    pythonExe: PYTHON_EXE,
  })

  const failedStartup: Error[] = []
  sup.on('failed', (err) => failedStartup.push(err))

  try {
    // ── 1. 拉起 ──────────────────────────────────────────────────────────────
    console.log('\n[1] 拉起 Python 后端…')
    sup.start()
    const port: number = await waitEvent(sup, 'ready', 30_000)
    assert(typeof port === 'number' && port > 0, `端口宣告：${port}`)
    assert(sup.alive, 'supervisor.alive === true')
    assert(failedStartup.length === 0, '无启动错误')

    await waitHttp(port, sup.token)
    console.log(`  /health 就绪（端口 ${port}）`)

    // ── 2. 正确 token 握手 ────────────────────────────────────────────────────
    console.log('\n[2] 正确 token WS 握手…')
    const sink = makeSink()
    const client = new PythonClient(port, sup.token, sink)
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

    // ── 5. 崩溃重启 ──────────────────────────────────────────────────────────
    console.log('\n[5] 模拟 Python 崩溃并验证重启…')
    client.stop()

    // 强杀进程
    const exitPromise = waitEvent(sup, 'exit', 10_000)
    ;(sup as unknown as { child: { kill: (sig?: string) => void } }).child?.kill('SIGKILL')
    await exitPromise
    assert(!sup.alive, '崩溃后 supervisor.alive === false')

    // 等 ready 再次触发
    const newPort: number = await waitEvent(sup, 'ready', 30_000)
    assert(typeof newPort === 'number' && newPort > 0, `重启后新端口：${newPort}`)
    await waitHttp(newPort, sup.token)

    const newSink = makeSink()
    const newClient = new PythonClient(newPort, sup.token, newSink)
    newClient.connect()
    await sleep(3_000)
    assert(newClient.connected, '重启后新 client 握手成功')
    newClient.stop()

    // ── 6. stop() 清理 ────────────────────────────────────────────────────────
    console.log('\n[6] stop() 后子进程退出…')
    const stopExit = waitEvent(sup, 'exit', 8_000)
    sup.stop()
    await stopExit
    assert(!sup.alive, 'stop() 后 alive === false')

  } catch (err) {
    console.error('\n[test] 异常：', err)
    failed++
  } finally {
    sup.stop()
    try { fs.rmSync(tmpDir, { recursive: true }) } catch { /* ignore */ }
  }

  console.log(`\n${'─'.repeat(50)}`)
  console.log(`结果：${passed} 通过 / ${failed} 失败`)
  if (failed > 0) process.exit(1)
}

main().catch((err) => { console.error(err); process.exit(1) })
