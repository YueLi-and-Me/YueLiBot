/**
 * 按钮基础组件，覆盖管理面板全部按钮形态。
 *
 * 基座触感按压设计：按钮站在墨色实心底边基座上，悬停时上浮 2px（底边同步
 * 加深）、按下时沉入基座，形成有物理感的按压反馈；ghost 变体无基座，用于工具
 * 栏轻量操作。变体语义：primary 主色实心、secondary 白底次操作、ghost 无框、
 * danger-outline 危险描边、danger 危险实心（确认弹窗用）。禁用态降透明度并
 * 阻断指针事件。
 */
import { cva, type VariantProps } from 'class-variance-authority'
import type { ButtonHTMLAttributes, ReactNode } from 'react'

import { cn } from './cn'

const buttonVariants = cva(
  'inline-flex cursor-pointer select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-xl border-[1.5px] font-semibold transition-[transform,box-shadow,background-color,color,filter] duration-100 disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        primary:
          'border-ink bg-primary text-primary-foreground shadow-press hover:-translate-y-0.5 hover:shadow-press-hover hover:brightness-105 active:translate-y-[1.5px] active:shadow-press-active',
        secondary:
          'border-ink bg-card text-foreground shadow-press hover:-translate-y-0.5 hover:bg-accent hover:text-accent-foreground hover:shadow-press-hover active:translate-y-[1.5px] active:shadow-press-active',
        ghost:
          'border-transparent text-muted-foreground hover:bg-accent hover:text-accent-foreground',
        'danger-outline':
          'border-destructive bg-card text-destructive shadow-[0_3px_0_hsl(var(--destructive))] hover:-translate-y-0.5 hover:bg-destructive-soft hover:shadow-[0_5px_0_hsl(var(--destructive))] active:translate-y-[1.5px] active:shadow-[0_1px_0_hsl(var(--destructive))]',
        danger:
          'border-ink bg-destructive text-destructive-foreground shadow-press hover:-translate-y-0.5 hover:shadow-press-hover hover:brightness-105 active:translate-y-[1.5px] active:shadow-press-active',
      },
      size: {
        sm: 'h-8 px-3 text-[13px]',
        md: 'h-9 px-4 text-sm',
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
 * @param props.size 尺寸档位，默认值为 `md`。
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
