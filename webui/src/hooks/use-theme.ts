/**
 * React 侧的主题读取与切换 hook。
 *
 * 主题的唯一事实来源是 `<html data-theme>`（由 lib/theme.ts 的 initTheme 在挂载
 * 前写入）：本 hook 通过 MutationObserver 订阅该属性，侧栏、窄屏抽屉与登录页
 * 等多处开关因此始终显示同一状态，系统偏好触发的自动切换也会同步到界面。
 */
import { useCallback, useSyncExternalStore } from 'react'

import { applyTheme, type ThemeName } from '@/lib/theme'

/** useTheme 的返回值结构。 */
interface ThemeState {
  /** 当前生效的主题名。 */
  theme: ThemeName
  /** 是否为暗色主题，供开关组件直接使用。 */
  dark: boolean
  /** 在明暗主题之间切换。 */
  toggle: () => void
}

/**
 * 订阅 `<html data-theme>` 的变化。
 *
 * @param onChange 属性变化时的通知回调。
 * @returns 取消订阅函数。
 */
function subscribe(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange)
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  return () => observer.disconnect()
}

/** 读取当前生效的主题名。 */
function currentTheme(): ThemeName {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'
}

/**
 * 读取当前主题并提供切换方法。
 *
 * @returns 主题状态与切换函数。
 */
export function useTheme(): ThemeState {
  const theme = useSyncExternalStore(subscribe, currentTheme)
  const toggle = useCallback(() => {
    applyTheme(currentTheme() === 'dark' ? 'light' : 'dark')
  }, [])
  return { theme, dark: theme === 'dark', toggle }
}
