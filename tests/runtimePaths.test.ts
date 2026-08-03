import { describe, expect, it } from 'vitest'

import { resolveRuntimePaths } from '../src/main/runtimePaths.ts'

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
