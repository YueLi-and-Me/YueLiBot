/**
 * 卡片与分区标题组件，是观察面板内容分区的基础容器。
 *
 * Card 提供白底、海军蓝硬描边、大圆角与实心基座的统一表面，悬停时微微抬起
 * （基座加高 + 轻位移），呼应按压设计语言中的「抬起/沉入」两态；SectionHeading
 * 渲染「图标色块 + 标题 + 副标题 + 可选工具区」的分区头部，图标色块使用四个
 * 协调色调（樱粉/天蓝/薄荷/薰衣草）之一，呼应粉白蓝主色的系谱。
 */
import type { HTMLAttributes, ReactNode } from 'react'

import { cn } from './cn'

/** 分区标题图标可选色调，与 index.css 中的 tint 令牌一一对应。 */
export type HeadingTint = 'coral' | 'amber' | 'olive' | 'plum'

const TINT_CLASSES: Record<HeadingTint, string> = {
  coral: 'bg-tint-coral/10 text-tint-coral',
  amber: 'bg-tint-amber/10 text-tint-amber',
  olive: 'bg-tint-olive/10 text-tint-olive',
  plum: 'bg-tint-plum/10 text-tint-plum',
}

interface CardProps extends HTMLAttributes<HTMLElement> {
  children: ReactNode
}

/**
 * 渲染内容分区卡片容器。
 *
 * @param props.className 追加类名，常用于 grid 跨度控制。
 * @returns section 卡片元素。
 */
export function Card({ className, children, ...rest }: CardProps) {
  return (
    <section
      className={cn(
        'rounded-xl border-[1.5px] border-ink bg-card text-card-foreground shadow-card',
        'transition-[transform,box-shadow] duration-200 ease-out hover:-translate-y-0.5 hover:shadow-lifted',
        className,
      )}
      {...rest}
    >
      {children}
    </section>
  )
}

interface SectionHeadingProps {
  /** 分区标题文本。 */
  title: string
  /** 分区副标题或标识说明，可选。 */
  subtitle?: string
  /** 标题左侧的图标色块内容（通常为 lucide 图标），可选。 */
  icon?: ReactNode
  /** 图标色块色调，默认值为 `blue`。 */
  tint?: HeadingTint
  /** 头部右侧工具区内容（过滤器、按钮等），可选。 */
  actions?: ReactNode
}

/**
 * 渲染分区卡片的标题栏。
 *
 * @param props.title 分区标题。
 * @param props.subtitle 副标题说明。
 * @param props.icon 图标节点。
 * @param props.tint 图标色调。
 * @param props.actions 右侧工具区节点。
 * @returns header 元素；工具区存在时自动两端对齐。
 */
export function SectionHeading({ title, subtitle, icon, tint = 'coral', actions }: SectionHeadingProps) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-4">
      <div className="flex min-w-0 items-center gap-2.5">
        {icon ? (
          <span
            className={cn(
              'grid size-8 flex-none place-items-center rounded-lg [&>svg]:size-4.5',
              TINT_CLASSES[tint],
            )}
            aria-hidden="true"
          >
            {icon}
          </span>
        ) : null}
        <div className="min-w-0">
          <h2 className="truncate text-[15px] font-semibold">{title}</h2>
          {subtitle ? <p className="truncate text-xs text-muted-foreground">{subtitle}</p> : null}
        </div>
      </div>
      {actions ? <div className="flex flex-none flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  )
}

/**
 * 渲染卡片内容区的统一内边距容器。
 *
 * @param props.className 追加类名。
 * @returns div 内容容器。
 */
export function CardBody({ className, children, ...rest }: CardProps) {
  return (
    <div className={cn('px-5 py-4', className)} {...rest}>
      {children}
    </div>
  )
}
