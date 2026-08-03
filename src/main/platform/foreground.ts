import { basename } from 'node:path'
import { screen } from 'electron'

/** 前台窗口信息。标题只在本进程内用于截图匹配，绝不外传。 */
export interface ForegroundInfo {
  process: string
  title?: string
  fullscreen?: boolean
}

/**
 * 前台窗口探测。
 *
 * ★ platform 适配层：唯一碰系统 API 的地方。将来迁 Tauri 只需重写本文件。
 *
 * 刻意做成「坏了就降级」而不是「坏了就报错」：
 * active-win 是原生模块，在某些环境（缺权限、被杀软拦、ABI 不匹配）下会加载失败。
 * 而屏幕感知只是锦上添花 —— 为它让整个桌宠起不来是完全不划算的。
 * 失败时返回 null，上层归类成 idle，她照样能聊天。
 */

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
 * 没有直接的 API，只能拿窗口尺寸跟所在显示器比。留 2px 容差 ——
 * 有些全屏应用会差一两像素，卡死等于永远判不出全屏。
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

export async function readForeground(): Promise<ForegroundInfo | null> {
  const activeWin = await load()
  if (!activeWin) return null

  try {
    const w = await activeWin()
    if (!w?.owner) return null

    // owner.name 在 Windows 上是**显示名**（"Kimi"、"Google Chrome"），
    // 而分类表按 exe 文件名匹配。优先从可执行路径取，取不到才退回显示名
    const proc = w.owner.path ? basename(w.owner.path) : (w.owner.name ?? '')

    return {
      process: proc,
      // 标题只在本地做归类判断用，classify() 会把它消化掉、不往外传
      ...(w.title ? { title: w.title } : {}),
      // active-win 只能通过 bounds 猜全屏，分不出真全屏和无边框窗口化。
      // 默认静默以保守保护直播/录屏；用户主动关掉开关后不把 fullscreen 上报给 classify。
      fullscreen: process.env.AWARENESS_FULLSCREEN_SILENT !== '0' && isFullscreen(w.bounds),
    }
  } catch {
    // 单次读取失败（窗口正在切换、权限瞬时不足）不该拉黑整个功能
    return null
  }
}

/** 供自检：探测能力当前是否可用。 */
export async function foregroundAvailable(): Promise<boolean> {
  return (await load()) !== null
}

/**
 * 这个前台窗口是不是我们自己。
 *
 * 必须排除掉：你点她、打字的那一刻，前台窗口就是**她自己的窗口**，
 * 归类出来是「在用电脑做别的事」。她于是会在你正跟她说话时
 * 说出「你在忙别的呀」这种明显不对的话。
 *
 * 正确的语义是「你转向她之前在做什么」，所以读到自己时应当保留上一个状态。
 */
export function isSelfProcess(proc: string): boolean {
  const self = basename(process.execPath).toLowerCase().replace(/\.exe$/, '')
  return proc.toLowerCase().replace(/\.exe$/, '') === self
}
