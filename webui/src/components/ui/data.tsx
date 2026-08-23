/**
 * 数据展示基础组件：指标行、进度条、标签片段与空/错误/加载状态。
 *
 * 指标行采用「左侧 muted 名称 + 右侧等宽数字值」的双栏排布；进度条用原生
 * progress 语义的重绘实现，保证无障碍名称可读；状态组件统一空数据、加载中
 * 与错误提示的呈现。进度条为樱粉填充，呼应设计语言主色。
 */
import { LoaderCircle } from 'lucide-react'
import type { ReactNode } from 'react'

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
