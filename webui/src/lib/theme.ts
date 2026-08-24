/**
 * 界面主题（明/暗）的解析、应用与持久化。
 *
 * 主题写入 `<html data-theme>` 属性并持久化到 localStorage；`initTheme` 在
 * React 挂载前同步执行，避免首屏按错误主题渲染后再闪烁。系统偏好变化只在
 * 用户未显式选择主题时生效，不覆盖手动切换结果。
 * 被 main.ts（首屏初始化）与 hooks/use-theme.ts（React 侧切换）依赖。
 */

/** 主题本地存储键名。 */
const THEME_STORAGE_KEY = 'yueli-webui-theme'

export type ThemeName = 'light' | 'dark'

/**
 * 读取本地存储的主题偏好。
 *
 * @returns 保存的主题名；未保存、值非法或读取失败（如隐私模式）时返回 `null`。
 */
export function savedTheme(): ThemeName | null {
  try {
    const value = localStorage.getItem(THEME_STORAGE_KEY)
    return value === 'light' || value === 'dark' ? value : null
  } catch {
    return null
  }
}

/** 换肤帧守卫的 rAF 句柄；连续快速切换时取消上一轮的移除计划。 */
let flipGuardFrame: number | null = null

/**
 * 应用界面主题并写入本地存储。
 *
 * @param theme 目标主题名。
 * @returns 无返回值；存储写入失败不阻断切换，本次会话内仍生效。
 * @remarks 翻转前先给 <html> 挂上 theme-flip 守卫类：禁用全页过渡与动画，
 * 让全部令牌变化在一帧内一次性生效（对应样式见 index.css 末尾，守卫会
 * 保留日月开关自身的动画）；连续两帧后移除，恢复日常微交互。首屏初始化
 * 时同样经过本守卫，顺带消除初始渲染的过渡抖动。
 */
export function applyTheme(theme: ThemeName): void {
  const root = document.documentElement
  root.classList.add('theme-flip')
  root.dataset.theme = theme
  if (flipGuardFrame !== null) cancelAnimationFrame(flipGuardFrame)
  flipGuardFrame = requestAnimationFrame(() => {
    flipGuardFrame = requestAnimationFrame(() => {
      root.classList.remove('theme-flip')
      flipGuardFrame = null
    })
  })
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    // 隐私模式下写入失败只影响持久化，主题在本次会话内已生效。
  }
}

/**
 * 计算当前应生效的主题。
 *
 * @returns 显式保存的主题；没有保存值时按系统 prefers-color-scheme 推导。
 */
export function resolveTheme(): ThemeName {
  return savedTheme() ?? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
}

/**
 * 在 React 挂载前初始化主题，并监听系统偏好变化。
 *
 * @returns 无返回值。
 * @remarks 系统偏好变化仅在用户未显式保存主题时应用，行为与旧版一致。
 */
export function initTheme(): void {
  applyTheme(resolveTheme())
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (savedTheme() === null) applyTheme(resolveTheme())
  })
}
