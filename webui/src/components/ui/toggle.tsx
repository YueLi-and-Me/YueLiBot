/**
 * 小型开关组件，用于「自动刷新」等布尔控制项。
 *
 * 以 `role="switch"` 的按钮实现，轨道/滑块全部为主色语义色；滑块位移使用
 * 全站统一的强调缓动曲线，比主题开关更小巧，适合工具栏内嵌。
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
        'inline-flex cursor-pointer items-center gap-2 text-[13px] text-muted-foreground transition-colors hover:text-foreground',
        className,
      )}
    >
      <span
        className={cn(
          'relative inline-flex h-[22px] w-10 flex-none items-center rounded-full border-[1.5px] border-ink/50 transition-colors duration-200',
          checked ? 'bg-primary' : 'bg-muted',
        )}
        aria-hidden="true"
      >
        <span
          className={cn(
            'inline-block size-3.5 rounded-full border border-ink/40 bg-white transition-transform duration-200 ease-[cubic-bezier(0.22,1,0.36,1)]',
            checked ? 'translate-x-[20px]' : 'translate-x-[2.5px]',
          )}
        />
      </span>
      {label}
    </button>
  )
}
