/**
 * Electron 与 Python 运行时目录解析测试。
 *
 * 本模块属于 Electron 主进程运行时路径层的 Vitest 测试，验证配置、数据库、缓存、
 * 临时目录和崩溃转储均解析到项目根目录下，并拒绝未显式授权的 C 盘路径。
 * 被测实现位于 electron/main/runtimePaths.ts。
 */
import { describe, expect, it } from 'vitest'

import { resolveRuntimePaths } from '../electron/main/runtimePaths.ts'

describe('项目内运行时路径', () => {
  it('把配置、数据库和 Electron 缓存全部放在项目目录', () => {
    const paths = resolveRuntimePaths('D:\\YueLiBot')

    expect(paths.configDir).toBe('D:\\YueLiBot\\config')
    expect(paths.dataDir).toBe('D:\\YueLiBot\\data')
    expect(paths.electronUserDataDir).toBe('D:\\YueLiBot\\data\\electron')
    expect(paths.electronSessionDataDir).toBe('D:\\YueLiBot\\data\\electron-session')
    expect(paths.electronTempDir).toBe('D:\\YueLiBot\\data\\temp')
    expect(paths.electronCrashDumpsDir).toBe('D:\\YueLiBot\\data\\crash-dumps')
  })

  it('拒绝把运行时目录解析到 C 盘', () => {
    expect(() => resolveRuntimePaths('C:\\Program Files\\YueLiBot')).toThrow(
      '拒绝把 YueLiBot 运行时文件写入 C 盘',
    )
  })

  it('允许通过环境变量显式指定其它盘的项目根目录', () => {
    const paths = resolveRuntimePaths('C:\\Program Files\\YueLiBot', 'E:\\Apps\\YueLiBot')

    expect(paths.projectRoot).toBe('E:\\Apps\\YueLiBot')
  })
})
