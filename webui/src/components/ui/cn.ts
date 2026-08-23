/**
 * 组合 Tailwind 类名的工具函数。
 *
 * 基于 clsx + tailwind-merge：前者过滤条件类名中的假值，后者解决 Tailwind
 * 类冲突（后者覆盖前者）；通过 extendTailwindMerge 注册本项目的自定义语义
 * 色/阴影/动画令牌，使 bg-card、shadow-card、animate-rise 等自定义类也参与
 * 冲突合并。被 components/ui 下各组件与业务分区组件依赖。
 *
 * @param inputs 待组合的类名，允许 `false`、`null`、`undefined` 占位。
 * @returns 合并后的类名字符串。
 */
import { clsx, type ClassValue } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      // 自定义语义色（index.css @theme inline 中的 --color-*），注册后
      // bg-/text-/border-/ring- 等颜色类组才能正确去重
      color: [
        'background',
        'foreground',
        'card',
        'card-foreground',
        'muted',
        'muted-foreground',
        'primary',
        'primary-foreground',
        'primary-soft',
        'secondary',
        'secondary-foreground',
        'accent',
        'accent-foreground',
        'destructive',
        'destructive-foreground',
        'destructive-soft',
        'success',
        'success-soft',
        'warning',
        'warning-soft',
        'border',
        'input',
        'ring',
        'sidebar',
        'sidebar-foreground',
        'sidebar-muted',
        'sidebar-border',
        'sidebar-active',
        'sidebar-active-foreground',
        'terminal',
        'terminal-foreground',
        'terminal-border',
        'tint-blue',
        'tint-cyan',
        'tint-teal',
        'tint-violet',
      ],
      // 自定义阴影与动画令牌
      shadow: ['card', 'lifted', 'dialog'],
      animate: ['fade-in', 'rise', 'scale-in', 'toast-in', 'pulse-dot', 'spin-slow'],
    },
  },
})

export function cn(...inputs: Array<ClassValue>): string {
  return twMerge(clsx(inputs))
}
