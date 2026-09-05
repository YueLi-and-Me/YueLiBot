/**
 * BackendLink 接管与重连行为测试。
 *
 * 本模块属于 Electron 主进程后端连接层的 Vitest 测试，覆盖入口反转之后的四条语义：
 * 读到有效凭据且健康检查通过即接管、凭据无效时不接管、超过连接窗口报告不可用、
 * 以及最关键的一条——本层永远不创建子进程。测试注入临时运行时文件与 fetch 替身，
 * 不启动真实进程。
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { BackendLink } from '../electron/main/python/backendLink.ts'

const TOKEN = 'a'.repeat(64)
const PORT = 51_234

/** 记录所有创建目录，afterEach 统一清理。 */
const createdDirs: string[] = []

/**
 * 创建一个带运行时凭据文件的临时数据目录。
 *
 * @param {unknown} payload 写入 runtime/backend.json 的内容；传 null 表示不写文件。
 * @returns {string} 临时数据目录路径。
 * @sideEffects 在系统临时目录下创建目录与文件，并登记待清理路径。
 */
function makeDataDir(payload: unknown): string {
  const dataDir = mkdtempSync(join(tmpdir(), 'yueli-backend-link-'))
  createdDirs.push(dataDir)
  if (payload !== null) {
    mkdirSync(join(dataDir, 'runtime'), { recursive: true })
    writeFileSync(join(dataDir, 'runtime', 'backend.json'), JSON.stringify(payload), 'utf-8')
  }
  return dataDir
}

/**
 * 用固定响应替换全局 fetch。
 *
 * @param {boolean} healthy 健康检查是否通过。
 * @returns {ReturnType<typeof vi.fn>} 替身函数，可用于断言调用地址。
 * @sideEffects 覆盖全局 fetch，由 afterEach 还原。
 */
function stubHealth(healthy: boolean) {
  const fetchMock = vi.fn(
    async (input: string, _init?: RequestInit) =>
      new Response('{}', { status: healthy ? 200 : 503 }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/**
 * 等待条件成立或超时。
 *
 * @param {() => boolean} done 返回是否完成的检查函数。
 * @param {number} [timeoutMs=3000] 最大等待毫秒数。
 * @returns {Promise<void>} 条件成立后完成。
 * @throws {Error} 超时仍未成立时抛出。
 */
async function waitFor(done: () => boolean, timeoutMs = 3_000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (!done()) {
    if (Date.now() > deadline) throw new Error('等待条件成立超时')
    await new Promise((resolve) => setTimeout(resolve, 10))
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  while (createdDirs.length > 0) {
    rmSync(createdDirs.pop()!, { recursive: true, force: true })
  }
})

describe('BackendLink', () => {
  it('凭据有效且健康检查通过时接管已运行的后端', async () => {
    const dataDir = makeDataDir({ port: PORT, token: TOKEN })
    const fetchMock = stubHealth(true)
    vi.spyOn(console, 'log').mockImplementation(() => undefined)
    const link = new BackendLink({ dataDir })
    const ready: Array<[number, string]> = []
    link.on('ready', (port, token) => ready.push([port, token]))

    try {
      link.start()
      await waitFor(() => ready.length > 0)

      expect(ready[0]).toEqual([PORT, TOKEN])
      expect(link.alive).toBe(true)
      expect(String(fetchMock.mock.calls[0]?.[0]))
        .toBe(`http://127.0.0.1:${PORT}/runtime/health`)
    } finally {
      link.stop()
    }
  })

  it('健康检查不过时不接管，超过连接窗口后报告不可用', async () => {
    const dataDir = makeDataDir({ port: PORT, token: TOKEN })
    stubHealth(false)
    const link = new BackendLink({ dataDir, attachTimeout: 0 })
    const failures: Error[] = []
    link.on('unavailable', (error) => failures.push(error))

    try {
      link.start()
      await waitFor(() => failures.length > 0)

      expect(link.alive).toBe(false)
      // 只钉「提到了入口脚本」这一点：启动器写法（uv run / python）会随文档调整，
      // 断言到那一层会把一次文案微调变成一条失败。
      expect(failures[0]?.message).toContain('bot.py')
    } finally {
      link.stop()
    }
  })

  it('运行时文件缺失时不接管，也不抛异常', async () => {
    const dataDir = makeDataDir(null)
    const fetchMock = stubHealth(true)
    const link = new BackendLink({ dataDir, attachTimeout: 0 })
    const failures: Error[] = []
    link.on('unavailable', (error) => failures.push(error))

    try {
      link.start()
      await waitFor(() => failures.length > 0)

      // 没有凭据就不该发出任何健康请求。
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      link.stop()
    }
  })

  it('token 字段不是 64 位十六进制时拒绝接管', async () => {
    const dataDir = makeDataDir({ port: PORT, token: 'short-token' })
    const fetchMock = stubHealth(true)
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const link = new BackendLink({ dataDir, attachTimeout: 0 })
    const failures: Error[] = []
    link.on('unavailable', (error) => failures.push(error))

    try {
      link.start()
      await waitFor(() => failures.length > 0)

      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      link.stop()
    }
  })

  // 【关键】这条断言守的是入口反转本身：本层若恢复 spawn，桌宠开启时会出现
  // 「后端拉起外壳、外壳又拉起后端」的第二份进程，两侧还会各拉一个 QQ 适配器。
  it('源码中不含任何创建子进程的调用', async () => {
    const { readFileSync } = await import('node:fs')
    const source = readFileSync('electron/main/python/backendLink.ts', 'utf8')

    expect(source).not.toContain('child_process')
    expect(source).not.toContain('spawn(')
    expect(source).not.toContain('execFile(')
  })
})
