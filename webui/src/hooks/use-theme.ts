/**
 * React 侧的主题读取与切换 hook。
 *
 * 初始值直接读取 `<html data-theme>`（由 lib/theme.ts 的 initTheme 在挂载前
 * 写入），切换时同步更新 DOM 属性与本地存储；被布局侧栏与登录页的主题开关
 * 引用。
 */
import { useState } from 'react'

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
 * 读取当前主题并提供切换方法。
 *
 * @returns 主题状态与切换函数。
 */
export function useTheme(): ThemeState {
  const [theme, setTheme] = useState<ThemeName>(() =>
    document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light',
  )
  const toggle = () => {
    setTheme((current) => {
      const next: ThemeName = current === 'dark' ? 'light' : 'dark'
      applyTheme(next)
      return next
    })
  }
  return { theme, dark: theme === 'dark', toggle }
}
