/**
 * 计算项目根目录、配置目录、运行数据目录和 Electron 用户数据目录。
 *
 * 主进程启动、后端连接和设置读写共享本模块的路径约定，避免各处根据当前工作目录
 * 重复推导并产生不同的配置或数据位置。
 */
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
 *
 * @param appPath Electron 提供的应用路径；未指定 configuredRoot 时作为项目根目录候选。
 * @param configuredRoot 可选的显式项目根目录，去除首尾空白后必须指向非 C 盘路径。
 * @param configuredDataDir 可选的显式数据目录；由后端拉起本外壳时注入它实际使用的
 *   目录。两侧各自按项目根推导，会在后端用了自定义 `--data-dir` 时分叉成两个
 *   `memory.db`，且双方都「工作正常」，只是记忆对不上——这类分叉必须由单向注入排除。
 * @param configuredConfigDir 可选的显式配置目录；同样由后端注入。分叉的后果更直接：
 *   `[desktop_pet] enabled` 两边各读一份，会出现「后端认为桌宠开着、外壳认为关着」。
 * @returns {RuntimePaths} 项目根、配置、数据库、Electron 缓存、临时目录和崩溃转储目录。
 * @throws Error 解析后的项目根位于 C 盘时抛出，避免运行时文件写入系统盘。
 * @sideEffects 仅解析路径，不创建目录或写入文件。
 *
 * 只有用户通过 YUELI_PROJECT_ROOT 明确指定其它盘时才允许改变根目录，不回退到 AppData。
 */
export function resolveRuntimePaths(
  appPath: string,
  configuredRoot?: string,
  configuredDataDir?: string,
  configuredConfigDir?: string,
): RuntimePaths {
  const projectRoot = resolve(configuredRoot?.trim() || appPath)
  if (/^c:[\\/]/i.test(projectRoot)) {
    throw new Error(
      `拒绝把 YueLiBot 运行时文件写入 C 盘：${projectRoot}。`
      + '请把项目放到其它盘，或设置 YUELI_PROJECT_ROOT。',
    )
  }

  const dataDir = resolve(projectRoot, configuredDataDir?.trim() || 'data')
  return {
    projectRoot,
    configDir: resolve(projectRoot, configuredConfigDir?.trim() || 'config'),
    legacyConfigPath: join(projectRoot, 'config.toml'),
    dataDir,
    electronUserDataDir: join(dataDir, 'electron'),
    electronSessionDataDir: join(dataDir, 'electron-session'),
    electronTempDir: join(dataDir, 'temp'),
    electronCrashDumpsDir: join(dataDir, 'crash-dumps'),
  }
}
