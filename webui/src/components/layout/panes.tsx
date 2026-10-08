/**
 * 面板式布局的积木：面板组、单个面板、面板标题栏、工具栏、滚动区、底栏与空态。
 *
 * 工作区由若干面板横向排开，面板之间只用一条分隔线，不留间隙、不加阴影，因此相邻
 * 面板的顶边、底边天然对齐；各面板的标题栏统一高 48px，标题栏底边连成一条横线。
 * 五种页面模板都由这些积木拼出：
 * - 收件箱三栏：列表 Pane + 填充 Pane + 检查器 Pane；
 * - 列表加详情：列表 Pane + 填充 Pane；
 * - 表格页：Toolbar + ScrollArea + PaneFooter；
 * - 画布加检查器：填充 Pane（画布）+ 检查器 Pane；
 * - 设置表单：导航 Pane + 填充 Pane（底部带保存条）。
 *
 * 窄屏（lg 以下）面板改为纵向堆叠，由 Panes 整体滚动，各面板内的滚动区随内容伸长。
 */
import type { HTMLAttributes, ReactNode } from 'react'

import { cn } from '@/components/ui'

/** 面板宽度档位：列表 300px、分区导航 260px、检查器 320px，其余为填充剩余空间。 */
type PaneWidth = 'list' | 'nav' | 'inspector' | 'fill'

const PANE_CLASSES: Record<PaneWidth, string> = {
  list: 'border-b border-border lg:w-[300px] lg:flex-none lg:border-r lg:border-b-0',
  nav: 'border-b border-border lg:w-[260px] lg:flex-none lg:border-r lg:border-b-0',
  inspector: 'border-t border-border lg:w-[320px] lg:flex-none lg:border-t-0 lg:border-l',
  fill: 'min-w-0 lg:flex-1',
}

/**
 * 渲染横向排列的面板组。
 *
 * @param props.className 追加类名。
 * @returns 填满正文区域的容器；桌面端各面板各自滚动，窄屏整体滚动。
 */
export function Panes({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div className={cn('flex min-h-0 flex-1 flex-col overflow-y-auto lg:flex-row lg:overflow-hidden', className)}>
      {children}
    </div>
  )
}

interface PaneProps extends HTMLAttributes<HTMLElement> {
  /** 宽度档位，默认填充。 */
  width?: PaneWidth
  children: ReactNode
}

/**
 * 渲染单个面板。
 *
 * @param props.width 宽度档位。
 * @returns section 元素，纵向排列标题栏、滚动区与底栏。
 */
export function Pane({ width = 'fill', className, children, ...rest }: PaneProps) {
  return (
    <section data-pane className={cn('flex flex-col lg:min-h-0', PANE_CLASSES[width], className)} {...rest}>
      {children}
    </section>
  )
}

interface PaneHeaderProps {
  /** 左侧内容：标题、计数、下划线页签等。 */
  children: ReactNode
  /** 右侧操作，可选。 */
  actions?: ReactNode
  /** 追加类名。 */
  className?: string
}

/**
 * 渲染面板标题栏。
 *
 * @param props.children 左侧内容；左侧区域纵向撑满，下划线页签可落在底部分隔线上。
 * @param props.actions 右侧操作。
 * @returns 高 48px、带底部分隔线的标题栏。
 */
export function PaneHeader({ children, actions, className }: PaneHeaderProps) {
  return (
    <div
      className={cn(
        'flex h-12 flex-none items-center justify-between gap-3 border-b border-border px-6',
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-2 self-stretch">{children}</div>
      {actions ? <div className="flex flex-none items-center gap-2">{actions}</div> : null}
    </div>
  )
}

/**
 * 渲染面板标题文本。
 *
 * @param props.children 标题。
 * @param props.count 标题旁的弱色计数，可选。
 * @returns 标题片段。
 */
export function PaneTitle({ children, count }: { children: ReactNode; count?: ReactNode }) {
  return (
    <>
      <h2 className="truncate text-[13px] font-semibold">{children}</h2>
      {count !== undefined ? <span className="text-xs text-subtle-foreground tabular-nums">{count}</span> : null}
    </>
  )
}

/**
 * 渲染工具栏：筛选、搜索、排序等控件排成一行，放不下时换行。
 *
 * @param props.className 追加类名。
 * @returns 带底部分隔线的工具栏，总高 48px（含分隔线），与面板标题栏等高；控件换行时
 * 随内容增高。上下内边距取 7px：32px 控件加两侧 7px 再加 1px 分隔线正好落在 48px 内，
 * 取 8px 会多出 1px，与相邻面板的标题栏底线错开。
 */
export function Toolbar({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div
      className={cn(
        'flex min-h-12 flex-none flex-wrap items-center gap-2 border-b border-border px-6 py-[7px]',
        className,
      )}
    >
      {children}
    </div>
  )
}

/**
 * 渲染滚动区。
 *
 * @param props.className 追加类名。
 * @returns 占满剩余高度并纵向滚动的容器；带 `data-scroll-area` 供翻页条定位。
 */
export function ScrollArea({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div data-scroll-area className={cn('min-h-0 flex-1 overflow-y-auto overscroll-contain', className)}>
      {children}
    </div>
  )
}

/**
 * 渲染面板底栏：翻页条、保存条等。
 *
 * @param props.className 追加类名。
 * @returns 高 48px、带顶部分隔线的底栏。
 */
export function PaneFooter({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div className={cn('flex min-h-12 flex-none items-center gap-3 border-t border-border px-6 py-[7px]', className)}>
      {children}
    </div>
  )
}

/**
 * 渲染面板内的一个小节：弱色小标题 + 内容，小节之间以分隔线隔开。
 *
 * @param props.title 小节标题。
 * @param props.actions 标题右侧的操作，可选。
 * @returns 小节元素；用于检查器等窄面板。
 */
export function PaneSection({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="border-b border-border px-6 py-4 last:border-b-0">
      <div className="mb-2.5 flex items-center justify-between gap-2">
        <h3 className="text-[11px] font-semibold tracking-[0.04em] text-subtle-foreground">{title}</h3>
        {actions}
      </div>
      {children}
    </div>
  )
}

/**
 * 渲染面板空态：没有选中项或没有内容时，在面板中央给出一句提示。
 *
 * @param props.icon 提示图标，可选。
 * @param props.children 提示文本。
 * @returns 居中的提示块。
 */
export function PaneEmpty({ icon, children }: { icon?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-h-48 flex-1 flex-col items-center justify-center gap-2 px-6 py-10 text-center text-[13px] text-muted-foreground">
      {icon ? <span className="text-subtle-foreground [&>svg]:size-6">{icon}</span> : null}
      {children}
    </div>
  )
}
