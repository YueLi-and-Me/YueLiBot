/**
 * 分段选项卡组件，替代各页面手写的 tab 条。
 *
 * 容器为 muted 底胶囊，激活项使用 motion 的 `layoutId` 共享元素转场——白色
 * 指示块在选项间滑动而非瞬移，是全站统一的手感来源；reduced-motion 由 App
 * 根部的 MotionConfig 自动降级为瞬移。
 */
import { LayoutGroup, motion } from 'motion/react'
import { useId } from 'react'

import { cn } from './cn'

interface SegmentedTab<T extends string> {
  /** 选项值。 */
  value: T
  /** 选项展示文本。 */
  label: string
}

interface SegmentedTabsProps<T extends string> {
  /** 选项列表，按展示顺序排列。 */
  tabs: Array<SegmentedTab<T>>
  /** 当前激活值。 */
  value: T
  /** 切换回调，参数为目标选项值。 */
  onChange: (value: T) => void
  /** 追加类名；需要整行宽度时传 `w-full` 并给每个选项加 `flex-1`。 */
  className?: string
  /** 选项按钮的追加类名，例如 `flex-1` 均分宽度。 */
  tabClassName?: string
}

/**
 * 渲染分段选项卡。
 *
 * @param props.tabs 选项列表。
 * @param props.value 当前值。
 * @param props.onChange 切换回调。
 * @returns tablist 容器。
 */
export function SegmentedTabs<T extends string>({
  tabs,
  value,
  onChange,
  className,
  tabClassName,
}: SegmentedTabsProps<T>) {
  // LayoutGroup 需要稳定 id，保证多个选项卡实例共存时指示器互不串扰
  const groupId = useId()
  return (
    <LayoutGroup id={groupId}>
      <div
        role="tablist"
        className={cn('inline-flex items-center gap-1 rounded-lg bg-muted p-1', className)}
      >
        {tabs.map((tab) => {
          const active = tab.value === value
          return (
            <button
              key={tab.value}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => onChange(tab.value)}
              className={cn(
                'relative cursor-pointer rounded-md px-3 py-1.5 text-[13px] font-medium whitespace-nowrap transition-colors duration-150',
                active ? 'text-foreground' : 'text-muted-foreground hover:text-foreground',
                tabClassName,
              )}
            >
              {active ? (
                <motion.span
                  layoutId="segmented-tabs-pill"
                  className="absolute inset-0 rounded-md border-[1.5px] border-ink bg-card shadow-[0_2px_0_hsl(var(--ink))]"
                  transition={{ type: 'spring', stiffness: 480, damping: 38, mass: 0.6 }}
                  aria-hidden="true"
                />
              ) : null}
              <span className="relative z-10">{tab.label}</span>
            </button>
          )
        })}
      </div>
    </LayoutGroup>
  )
}
