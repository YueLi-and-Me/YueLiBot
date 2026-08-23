/**
 * 定制勾选框组件，替换裸露的浏览器原生 checkbox。
 *
 * 以 `role="checkbox"` 的按钮实现，选中态为主色底 + 白色对勾；与 Toggle 分工：
 * Checkbox 用于表单内的多选集合（如枚举多选字段），Toggle 用于即时生效的开关。
 */
import { Check } from 'lucide-react'
import type { ReactNode } from 'react'

import { cn } from './cn'

interface CheckboxProps {
  /** 当前勾选态。 */
  checked: boolean
  /** 状态变更回调，参数为目标状态。 */
  onChange: (next: boolean) => void
  /** 右侧标签内容，可选。 */
  label?: ReactNode
  /** 追加类名。 */
  className?: string
  /** 禁用态。 */
  disabled?: boolean
}

/**
 * 渲染定制风格的勾选框。
 *
 * @param props.checked 勾选态。
 * @param props.onChange 变更回调。
 * @param props.label 标签内容。
 * @returns 勾选框按钮元素。
 */
export function Checkbox({ checked, onChange, label, className, disabled }: CheckboxProps) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'inline-flex cursor-pointer items-center gap-2 text-sm text-foreground disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
    >
      <span
        className={cn(
          'inline-flex size-4 flex-none items-center justify-center rounded-[5px] border-[1.5px] transition-colors duration-150',
          checked
            ? 'border-ink bg-primary text-primary-foreground'
            : 'border-ink/50 bg-card hover:border-ink',
        )}
        aria-hidden="true"
      >
        {checked ? <Check className="size-3" strokeWidth={3.5} /> : null}
      </span>
      {label}
    </button>
  )
}
