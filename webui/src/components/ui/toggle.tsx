/**
 * 小型开关组件，用于「自动刷新」等布尔控制项。
 *
 * 以 `role="switch"` 的按钮实现，轨道开启时为主色、关闭时为控件描边色；滑块位移
 * 使用全站统一的强调缓动曲线。整体高 32px，可与按钮、输入框排在同一行。
 */
import { cn } from './cn'

interface ToggleProps {
  /** 当前开关态。 */
  checked: boolean
  /** 状态变更回调，参数为目标状态。 */
  onChange: (next: boolean) => void
  /** 开关右侧的文字标签。 */
  label: string
  /** 追加类名。 */
  className?: string
}

/**
 * 渲染带文字标签的小型开关。
 *
 * @param props.checked 开关态。
 * @param props.onChange 变更回调。
 * @param props.label 文字标签。
 * @returns 开关按钮元素。
 */
export function Toggle({ checked, onChange, label, className }: ToggleProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className={cn(
        'inline-flex h-8 cursor-pointer items-center gap-2 text-[13px] text-muted-foreground transition-colors hover:text-foreground',
        className,
      )}
    >
      <span
        className={cn(
          'relative inline-flex h-4 w-[30px] flex-none items-center rounded-full transition-colors duration-200',
          checked ? 'bg-primary' : 'bg-input',
        )}
        aria-hidden="true"
      >
        <span
          className={cn(
            'inline-block size-3 rounded-full bg-white shadow-xs transition-transform duration-200 ease-[cubic-bezier(0.22,1,0.36,1)]',
            checked ? 'translate-x-4' : 'translate-x-0.5',
          )}
        />
      </span>
      {label}
    </button>
  )
}
