import { join, resolve } from 'node:path'

export interface RuntimePaths {
  projectRoot: string
  configDir: string
  legacyConfigPath: string
  dataDir: string
  electronUserDataDir: string
  electronSessionDataDir: string
  electronTempDir: string
  electronCrashDumpsDir: string
}

/**
 * 运行时文件必须留在项目目录。若打包或启动方式把项目解析到 C 盘，直接报错，
 * 由用户通过 YUELI_PROJECT_ROOT 明确指定其它盘，绝不悄悄写回 AppData。
 */
export function resolveRuntimePaths(appPath: string, configuredRoot?: string): RuntimePaths {
  const projectRoot = resolve(configuredRoot?.trim() || appPath)
  if (/^c:[\\/]/i.test(projectRoot)) {
    throw new Error(
      `拒绝把 YueLiBot 运行时文件写入 C 盘：${projectRoot}。`
      + '请把项目放到其它盘，或设置 YUELI_PROJECT_ROOT。',
    )
  }

  const dataDir = join(projectRoot, 'data')
  return {
    projectRoot,
    configDir: join(projectRoot, 'config'),
    legacyConfigPath: join(projectRoot, 'config.toml'),
    dataDir,
    electronUserDataDir: join(dataDir, 'electron'),
    electronSessionDataDir: join(dataDir, 'electron-session'),
    electronTempDir: join(dataDir, 'temp'),
    electronCrashDumpsDir: join(dataDir, 'crash-dumps'),
  }
}
