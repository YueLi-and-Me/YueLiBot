/**
 * 表单控件基础组件：字段包装器、输入框、文本域与原生下拉框。
 *
 * 控件统一 12px 圆角与发丝描边；悬停时描边加深，聚焦时主色描边 + 双层柔光
 * 焦点环（叠加在全局 outline 焦点环之上，视觉更细腻）；Select 使用原生 select
 * 保证键盘与读屏行为，右侧内嵌 chevron 图标替换浏览器默认箭头。
 */
import { ChevronDown, Info } from 'lucide-react'
import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react'

import { cn } from './cn'

const CONTROL_CLASSES =
  'w-full rounded-lg border border-input bg-card px-3 text-sm text-foreground transition-[border-color,box-shadow] duration-150 placeholder:text-muted-foreground/60 hover:border-ring/45 focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/20 disabled:cursor-not-allowed disabled:opacity-60'

interface FieldProps {
  /** 字段标签文本；不传则不渲染 label。 */
  label?: string
  /** 关联控件的 id，用于 label 的可访问性绑定。 */
  htmlFor?: string
  /** 追加在字段容器上的类名。 */
  className?: string
  /** 标签右侧问号图标的原生提示文本。 */
  help?: string
  children: ReactNode
}

/**
 * 渲染「标签 + 控件」纵向排布的字段容器。
 *
 * @param props.label 标签文本。
 * @param props.htmlFor 控件 id。
 * @returns label+控件 的包裹元素。
 */
export function Field({ label, htmlFor, className, help, children }: FieldProps) {
  return (
    <div className={cn('flex min-w-0 flex-col gap-1', className)}>
      {label ? (
        <span className="flex items-center gap-1.5">
          <label htmlFor={htmlFor} className="text-xs font-medium text-muted-foreground">
            {label}
          </label>
          {help ? (
            <span title={help} className="inline-flex cursor-help">
              <Info
                className="size-3.5 text-muted-foreground/70 hover:text-foreground"
                aria-hidden="true"
              />
            </span>
          ) : null}
        </span>
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
