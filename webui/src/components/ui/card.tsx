/**
 * 卡片与分区标题组件，是各业务分区的基础容器。
 *
 * 卡片有两种呈现，由所在位置决定而不是由调用方逐个指定：
 * - boxed（默认）：圆角细描边的独立块，用于工作区里需要成组框起来的内容；
 * - flat：放进面板（列表、详情、检查器）时使用，去掉描边与圆角，只在分区之间
 *   保留一条分隔线，面板本身已经是容器，再套一层框会出现「框中框」。
 * 面板通过 FlatCards 包裹子树切换为 flat，业务组件无需改动即可复用。面板标题栏
 * 已经写明内容是什么时（例如页签切换的单个分区），可以再让分区标题只保留副标题
 * 与工具区，避免同一个名字出现两次。
 */
import { createContext, useContext, type HTMLAttributes, type ReactNode } from 'react'

import { cn } from './cn'

/** 分区标题图标可选色调，与 index.css 中的 tint 令牌一一对应。 */
export type HeadingTint = 'coral' | 'amber' | 'olive' | 'plum'

const TINT_CLASSES: Record<HeadingTint, string> = {
  coral: 'text-tint-coral',
  amber: 'text-tint-amber',
  olive: 'text-tint-olive',
  plum: 'text-tint-plum',
}

interface CardVariant {
  /** 是否为面板内的扁平呈现。 */
  flat: boolean
  /** 是否隐藏分区标题文本（只留副标题与工具区）。 */
  hideTitle: boolean
}

/** 卡片呈现方式；默认 boxed，FlatCards 子树内为 flat。 */
const CardVariantContext = createContext<CardVariant>({ flat: false, hideTitle: false })

/**
 * 把子树内的卡片切换为面板内的扁平呈现。
 *
 * @param props.hideTitles 是否隐藏分区标题文本，默认值为 `false`。
 * @param props.children 子树。
 * @returns 上下文提供者。
 */
export function FlatCards({ hideTitles = false, children }: { hideTitles?: boolean; children: ReactNode }) {
  return (
    <CardVariantContext.Provider value={{ flat: true, hideTitle: hideTitles }}>{children}</CardVariantContext.Provider>
  )
}

interface CardProps extends HTMLAttributes<HTMLElement> {
  children: ReactNode
}

/**
 * 渲染内容分区卡片容器。
 *
 * @param props.className 追加类名，常用于 grid 跨度控制。
 * @returns section 卡片元素。
 * @remarks `content-visibility: auto` 让屏外卡片完全跳过布局与绘制，长页面
 * （观察页常态七八千个 DOM 节点）滚动时每帧只栅格化可见卡片；
 * `contain-intrinsic-size` 用最近渲染尺寸兜底，避免滚动条随屏外卡片跳动。
 */
export function Card({ className, children, ...rest }: CardProps) {
  const { flat } = useContext(CardVariantContext)
  return (
    <section
      className={cn(
        !flat
          ? 'rounded-lg border border-border bg-card text-card-foreground'
          : 'border-b border-border bg-transparent text-card-foreground last:border-b-0',
        '[content-visibility:auto] [contain-intrinsic-size:auto_320px]',
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
  /** 标题左侧的图标（通常为 lucide 图标），可选。 */
  icon?: ReactNode
  /** 图标色调，默认值为 `coral`。 */
  tint?: HeadingTint
  /** 头部右侧工具区内容（过滤器、按钮等），可选。 */
  actions?: ReactNode
}

/**
 * 渲染分区卡片的标题栏。
 *
 * @param props.title 分区标题。
 * @param props.subtitle 副标题说明，紧跟标题以弱色显示。
 * @param props.icon 图标节点，以色调着色、不加底色块。
 * @param props.tint 图标色调。
 * @param props.actions 右侧工具区节点。
 * @returns header 元素；boxed 时高 48px 并带底部分隔线，与面板标题栏同高。
 */
export function SectionHeading({ title, subtitle, icon, tint = 'coral', actions }: SectionHeadingProps) {
  const { flat, hideTitle } = useContext(CardVariantContext)
  if (hideTitle && !subtitle && !actions) return null
  return (
    <header
      className={cn(
        'flex flex-wrap items-center justify-between gap-x-3 gap-y-2',
        !flat ? 'min-h-12 border-b border-border px-5 py-2' : 'px-6 pt-5 pb-2',
      )}
    >
      <div className="flex min-w-0 items-center gap-2">
        {hideTitle ? null : icon ? (
          <span className={cn('flex-none [&>svg]:size-4', TINT_CLASSES[tint])} aria-hidden="true">
            {icon}
          </span>
        ) : null}
        {hideTitle ? null : <h2 className="truncate text-[13px] font-semibold">{title}</h2>}
        {subtitle ? <p className="truncate text-xs text-subtle-foreground">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex flex-none flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  )
}

/**
 * 渲染卡片内容区的统一内边距容器。
 *
 * @param props.className 追加类名。
 * @returns div 内容容器；flat 时左右内边距与面板标题栏一致（24px）。
 */
export function CardBody({ className, children, ...rest }: CardProps) {
  const { flat } = useContext(CardVariantContext)
  return (
    <div className={cn(!flat ? 'px-5 py-4' : 'px-6 pt-1 pb-5', className)} {...rest}>
      {children}
    </div>
  )
}
