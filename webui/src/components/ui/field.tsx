/**
 * 表单控件基础组件：字段包装器、输入框、文本域与原生下拉框。
 *
 * 控件统一 8px 圆角、发丝描边与品牌蓝焦点态；Select 使用原生 select 保证
 * 键盘与读屏行为，右侧内嵌 chevron 图标替换浏览器默认箭头。
 */
import { ChevronDown } from 'lucide-react'
import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react'

import { cn } from './cn'

const CONTROL_CLASSES =
  'w-full rounded-md border border-input bg-card px-3 text-sm text-foreground shadow-card transition-colors duration-150 placeholder:text-muted-foreground/70 hover:border-ring/50 focus:border-ring focus:outline-none disabled:cursor-not-allowed disabled:opacity-60'

interface FieldProps {
  /** 字段标签文本；不传则不渲染 label。 */
  label?: string
  /** 关联控件的 id，用于 label 的可访问性绑定。 */
  htmlFor?: string
  /** 追加在字段容器上的类名。 */
  className?: string
  children: ReactNode
}

/**
 * 渲染「标签 + 控件」纵向排布的字段容器。
 *
 * @param props.label 标签文本。
 * @param props.htmlFor 控件 id。
 * @returns label+控件 的包裹元素。
 */
export function Field({ label, htmlFor, className, children }: FieldProps) {
  return (
    <div className={cn('flex min-w-0 flex-col gap-1', className)}>
      {label ? (
        <label htmlFor={htmlFor} className="text-xs font-medium text-muted-foreground">
          {label}
        </label>
      ) : null}
      {children}
    </div>
  )
}

/**
 * 渲染统一风格的单行输入框。
 *
 * @param props 原生 input 属性。
 * @returns input 元素，高度固定为 36px。
 */
export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(CONTROL_CLASSES, 'h-9', className)} {...rest} />
}

/**
 * 渲染统一风格的多行文本域。
 *
 * @param props 原生 textarea 属性。
 * @returns textarea 元素。
 */
export function Textarea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(CONTROL_CLASSES, 'py-2 leading-relaxed', className)} {...rest} />
}

/**
 * 渲染统一风格的原生下拉框。
 *
 * @param props.children option 元素列表。
 * @returns 带自定义 chevron 的 select 容器。
 * @remarks 右侧 chevron 为 lucide 内联 SVG，不影响原生交互；容器宽度随父级。
 */
export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <div className={cn('relative min-w-0', className)}>
      <select
        className={cn(CONTROL_CLASSES, 'h-9 cursor-pointer appearance-none pr-8', className)}
        {...rest}
      >
        {children}
      </select>
      <ChevronDown
        className="pointer-events-none absolute top-1/2 right-2.5 size-4 -translate-y-1/2 text-muted-foreground"
        aria-hidden="true"
      />
    </div>
  )
}
