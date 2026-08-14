/**
 * 按钮基础组件，覆盖观察面板全部按钮形态。
 *
 * 变体语义：primary 为品牌蓝主操作；secondary 为白底描边次操作；ghost 为无框
 * 轻量操作；danger-outline 为危险确认操作。所有变体统一 8px 圆角、中等字重，
 * 禁用态降透明度并阻断指针事件。
 */
import type { ButtonHTMLAttributes, ReactNode } from 'react'

import { cn } from './cn'

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger-outline'
type ButtonSize = 'sm' | 'md'

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    'bg-primary text-primary-foreground shadow-card hover:brightness-108 active:brightness-95',
  secondary:
    'border border-border bg-card text-foreground shadow-card hover:bg-accent hover:text-accent-foreground',
  ghost: 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
  'danger-outline':
    'border border-destructive/50 text-destructive hover:bg-destructive-soft',
}

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-[13px]',
  md: 'h-9 px-4 text-sm',
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** 按钮视觉变体，默认值为 `primary`。 */
  variant?: ButtonVariant
  /** 按钮尺寸档位，默认值为 `md`。 */
  size?: ButtonSize
  children: ReactNode
}

/**
 * 渲染统一风格的按钮。
 *
 * @param props.variant 视觉变体。
 * @param props.size 尺寸档位。
 * @param props.className 调用方追加的类名，优先级最高。
 * @returns 按钮元素。
 */
export function Button({
  variant = 'primary',
  size = 'md',
  className,
  type = 'button',
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={cn(
        'inline-flex cursor-pointer select-none items-center justify-center gap-1.5 rounded-md font-medium whitespace-nowrap transition-all duration-150',
        'disabled:pointer-events-none disabled:opacity-50',
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  )
}
