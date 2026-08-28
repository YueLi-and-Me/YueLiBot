/**
 * 数据展示基础组件：指标行、进度条、标签片段、翻页条与空/错误/加载状态。
 *
 * 指标行采用「左侧 muted 名称 + 右侧等宽数字值」的双栏排布；进度条用原生
 * progress 语义的重绘实现，保证无障碍名称可读；翻页条把「共 N 条 · 第 X / Y 页」
 * 与上下页按钮统一成一行，供各列表面板复用；状态组件统一空数据、加载中与错误
 * 提示的呈现。进度条为樱粉填充，呼应设计语言主色。
 */
import { LoaderCircle } from 'lucide-react'
import type { ReactNode } from 'react'

import { Button } from './button'
import { cn } from './cn'

interface MetricProps {
  /** 指标名称。 */
  label: string
  /** 指标展示值。 */
  value: ReactNode
  /** 指标补充说明，展示在值下方的小字，可选。 */
  detail?: string
}

/**
 * 渲染一行键值指标。
 *
 * @param props.label 指标名称。
 * @param props.value 指标值。
 * @param props.detail 补充说明。
 * @returns 指标行元素。
 */
export function Metric({ label, value, detail }: MetricProps) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="flex-none text-xs text-muted-foreground">{label}</span>
      <span className="min-w-0 text-right">
        <strong className="font-mono text-[13px] font-semibold tabular-nums">{value}</strong>
        {detail ? <span className="block text-[11px] text-muted-foreground">{detail}</span> : null}
      </span>
    </div>
  )
}

interface ProgressProps {
  /** 当前值；渲染时限制在 0 到 max 之间。 */
  value: number
  /** 最大值。 */
  max: number
  /** 进度条无障碍名称。 */
  label: string
  /** 追加类名。 */
  className?: string
}

/**
 * 渲染樱粉色进度条。
 *
 * @param props.value 当前值。
 * @param props.max 最大值。
 * @param props.label 无障碍名称。
 * @returns 进度条元素。
 */
export function Progress({ value, max, label, className }: ProgressProps) {
  const clamped = Math.min(max, Math.max(0, value))
  const percent = max > 0 ? (clamped / max) * 100 : 0
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={clamped}
      className={cn('h-1.5 w-full overflow-hidden rounded-full bg-muted', className)}
    >
      <div
        className="h-full rounded-full bg-primary transition-[width] duration-300 ease-out"
        style={{ width: `${percent}%` }}
      />
    </div>
  )
}

interface ChipProps {
  /** 标签名称。 */
  label: string
  /** 标签值。 */
  value: ReactNode
}

/**
 * 渲染标签式键值片段。
 *
 * @param props.label 标签名。
 * @param props.value 标签值。
 * @returns 胶囊形 chip 元素。
 */
export function Chip({ label, value }: ChipProps) {
  return (
    <span className="inline-flex max-w-full items-center gap-1 rounded-full border border-border/70 bg-secondary px-2.5 py-0.5 text-xs text-secondary-foreground">
      <strong className="font-medium">{label}</strong>
      <span className="truncate">{value}</span>
    </span>
  )
}

interface PagerProps {
  /** 当前页码，从 0 开始。 */
  page: number
  /** 总页数，最小为 1。 */
  pageCount: number
  /** 条目总数，用于「共 N 条」文案。 */
  total: number
  /** 翻页回调，入参为目标页码（0 起）。 */
  onChange: (page: number) => void
  /** 是否禁用翻页按钮，用于服务端分页在取新一页期间锁住操作，默认值为 `false`。 */
  disabled?: boolean
}

/**
 * 渲染列表翻页条：左侧总量与页码，右侧回到首页 / 上一页 / 下一页按钮。
 *
 * 三种形态按数据量退化：无数据时整条不渲染；只有一页时只留「共 N 条」，不显示
 * 「第 1 / 1 页」和几个恒禁用的按钮；多页时才是完整形态。
 *
 * 「回到第一页」在翻了很多页之后单独有用：靠「上一页」退回去要点很多次。它同时
 * 把窗口滚回顶部——翻页条在页尾，只换页不滚动会停在第一页的**底部**，看起来
 * 像没生效。
 *
 * @param props.page 当前页码，0 起。
 * @param props.pageCount 总页数。
 * @param props.total 条目总数。
 * @param props.onChange 翻页回调，入参为目标页码。
 * @param props.disabled 是否禁用翻页按钮。
 * @returns 翻页条元素；`total` 为 0 时返回 `null`。
 */
export function Pager({ page, pageCount, total, onChange, disabled = false }: PagerProps) {
  if (total <= 0) return null
  const paged = pageCount > 1
  return (
    <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
      <span className="tabular-nums">
        共 {total} 条{paged ? ` · 第 ${page + 1} / ${pageCount} 页` : ''}
      </span>
      {paged ? (
        <div className="flex gap-2">
          <Button
            variant="ghost"
            size="sm"
            disabled={disabled || page <= 0}
            onClick={() => {
              onChange(0)
              window.scrollTo({ top: 0, behavior: 'smooth' })
            }}
          >
            回到第一页
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={disabled || page <= 0}
            onClick={() => onChange(page - 1)}
          >
            上一页
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={disabled || page + 1 >= pageCount}
            onClick={() => onChange(page + 1)}
          >
            下一页
          </Button>
        </div>
      ) : null}
    </div>
  )
}

/**
 * 渲染居中的空数据提示。
 *
 * @param props.children 提示文本。
 * @returns 空态段落。
 */
export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-muted-foreground">{children}</p>
}

/**
 * 渲染错误提示文本。
 *
 * @param props.children 错误内容。
 * @returns 带 alert 语义的段落。
 */
export function ErrorText({ children }: { children: ReactNode }) {
  return (
    <p role="alert" className="py-2 text-sm text-destructive">
      {children}
    </p>
  )
}

/**
 * 渲染加载中状态（旋转图标 + 文本）。
 *
 * @param props.children 加载提示文本。
 * @returns 加载态段落。
 */
export function Loading({ children }: { children: ReactNode }) {
  return (
    <p className="flex items-center justify-center gap-2 py-6 text-sm text-muted-foreground">
      <LoaderCircle className="size-4 animate-spin-slow" aria-hidden="true" />
      {children}
    </p>
  )
}
