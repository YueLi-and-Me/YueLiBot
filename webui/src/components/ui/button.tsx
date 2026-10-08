/**
 * 按钮基础组件，覆盖管理面板全部按钮形态。
 *
 * 面板式视觉下按钮保持扁平：主操作为雾蓝实色，次操作为白底细描边，悬停只改底色，
 * 不做位移与投影，避免在密集的工具栏里抖动。变体语义：primary 主色实心、
 * secondary 白底次操作、ghost 无框、danger-outline 危险描边、danger 危险实心
 * （确认弹窗用）。禁用态降透明度并阻断指针事件。
 */
import { cva, type VariantProps } from 'class-variance-authority'
import type { ButtonHTMLAttributes, ReactNode } from 'react'

import { cn } from './cn'

const buttonVariants = cva(
  'inline-flex cursor-pointer select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-md border font-medium transition-colors duration-150 disabled:pointer-events-none disabled:opacity-50 [&>svg]:shrink-0',
  {
    variants: {
      variant: {
        primary: 'border-transparent bg-primary text-primary-foreground hover:bg-primary/90 active:bg-primary/85',
        secondary: 'border-input bg-card text-foreground hover:bg-muted active:bg-nav-active',
        ghost: 'border-transparent text-muted-foreground hover:bg-muted hover:text-foreground',
        'danger-outline': 'border-destructive/35 bg-card text-destructive hover:bg-destructive-soft',
        danger: 'border-transparent bg-destructive text-destructive-foreground hover:bg-destructive/90',
      },
      size: {
        sm: 'h-7 px-2.5 text-xs',
        md: 'h-8 px-3 text-[13px]',
      },
    },
    defaultVariants: {
      variant: 'primary',
      size: 'md',
    },
  },
)

interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  children: ReactNode
}

/**
 * 渲染统一风格的按钮。
 *
 * @param props.variant 视觉变体，默认值为 `primary`。
 * @param props.size 尺寸档位，默认值为 `md`（高 32px，与输入框等高）。
 * @param props.className 调用方追加的类名，冲突时优先级最高。
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
      className={cn(buttonVariants({ variant, size }), className)}
      {...rest}
    >
      {children}
    </button>
  )
}
