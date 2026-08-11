/**
 * 采集当前前台窗口的进程名、标题和全屏状态，并生成稳定的活动快照。
 *
 * 主进程使用本模块驱动前台活动轮询；标题仅用于本地截图匹配，发送到后端的
 * 活动数据由调用方筛选，不能替代视觉接口的隐私策略。
 */
import { basename } from 'node:path'
import { screen } from 'electron'

/** 前台窗口信息。标题只在本进程内用于截图匹配，绝不外传。 */
export interface ForegroundInfo {
  process: string
  title?: string
  fullscreen?: boolean
}

/** 前台探测只接触系统窗口 API；读取失败时返回空值，由上层使用 idle 状态。 */

type ActiveWinFn = () => Promise<
  | {
      title?: string
      bounds?: { x: number; y: number; width: number; height: number }
      owner?: { name?: string; path?: string }
    }
  | undefined
>

let loader: Promise<ActiveWinFn | null> | null = null
let failed = false

/**
 * 延迟加载 active-win 原生模块并缓存加载结果。
 *
 * @returns 可调用的前台窗口探测函数；模块缺失、权限不足或加载失败时返回
 * ``null``，后续调用不重复加载失败模块。
 * @sideEffects 最多执行一次动态导入并记录失败状态。
 */
async function load(): Promise<ActiveWinFn | null> {
  if (failed) return null
  loader ??= import('active-win')
    .then((m) => (m.default ?? m) as unknown as ActiveWinFn)
    .catch((err) => {
      failed = true
      console.warn('[awareness] 前台窗口探测不可用，屏幕感知已关闭：', err instanceof Error ? err.message : err)
      return null
    })
  return loader
}

/**
 * 判断是否全屏。
 *
 * @param bounds 可选窗口边界；缺省时视为非全屏，坐标和尺寸单位为屏幕像素。
 * @returns 窗口尺寸在所在显示器边界 2 像素容差内时返回 ``true``，否则返回 ``false``。
 * @sideEffects 读取 Electron 当前显示器信息，不修改窗口或进程状态。
 *
 * 没有统一的全屏 API，只能将窗口尺寸与所在显示器比较；保留 2px 容差，
 * 避免边框或缩放误差使真实全屏窗口长期无法识别。
 */
function isFullscreen(bounds?: { x: number; y: number; width: number; height: number }): boolean {
  if (!bounds) return false
  try {
    const display = screen.getDisplayMatching(bounds)
    return bounds.width >= display.bounds.width - 2 && bounds.height >= display.bounds.height - 2
  } catch {
    return false
  }
}

/**
 * 读取当前前台窗口并返回经过隐私筛选的活动信息。
 *
 * @param fullscreenSilent 是否将接近显示器尺寸的窗口标记为全屏并触发静默策略。
 * @returns 前台进程、可选标题和全屏状态；探测器不可用、无窗口或单次读取失败
 * 时返回 ``null``。
 * @sideEffects 可能动态加载原生探测模块；窗口标题只在本地返回给调用方，不负责
 * 持久化或网络发送。
 */
export async function readForeground(fullscreenSilent: boolean): Promise<ForegroundInfo | null> {
  const activeWin = await load()
  if (!activeWin) return null

  try {
    const w = await activeWin()
    if (!w?.owner) return null

    // 分类表按可执行文件名匹配，因此优先使用 owner.path，路径缺失时才使用显示名。
    const proc = w.owner.path ? basename(w.owner.path) : (w.owner.name ?? '')

    return {
      process: proc,
      // 标题只供本地分类使用，后续分类层负责决定是否暴露应用描述。
      ...(w.title ? { title: w.title } : {}),
      // bounds 只能近似判断全屏；启用静默策略时才将该结果交给上层。
      fullscreen: fullscreenSilent && isFullscreen(w.bounds),
    }
  } catch {
    // 窗口切换或临时权限错误只影响本次采样，不改变模块加载成功状态。
    return null
  }
}

/**
 * 检查前台窗口探测模块当前是否可用。
 *
 * @returns 动态模块已成功加载时为 ``true``，否则为 ``false``。
 */
export async function foregroundAvailable(): Promise<boolean> {
  return (await load()) !== null
}

/**
 * 判断前台进程是否为当前 Electron 应用本身。
 *
 * @param proc 前台探测器返回的进程名或可执行文件名。
 * @returns 去除 ``.exe`` 后与当前 Electron 进程名相同时为 ``true``。
 * @sideEffects 不修改进程或窗口状态。
 */
export function isSelfProcess(proc: string): boolean {
  const self = basename(process.execPath).toLowerCase().replace(/\.exe$/, '')
  return proc.toLowerCase().replace(/\.exe$/, '') === self
}
