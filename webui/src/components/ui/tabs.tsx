/**
 * 选项卡组件：分段控件与下划线页签两种形态。
 *
 * - SegmentedTabs：灰底胶囊里的分段按钮，用于筛选、排序这类「同一份数据换个
 *   口径看」的切换，选中块通过 motion 的 `layoutId` 在选项间滑动；
 * - UnderlineTabs：放在面板标题栏里的下划线页签，用于切换面板正文的内容，
 *   下划线与标题栏底部分隔线重合。
 * reduced-motion 由 App 根部的 MotionConfig 自动降级为瞬移。
 *
 * 点击已激活项同样会触发 `onChange`：排序控件据此用两个按钮表达四种口径
 * （再点一次翻方向），调用方需要自己判断是否为同值点击。
 */
import { LayoutGroup, motion } from 'motion/react'
import { useId, type ReactNode } from 'react'

import { cn } from './cn'

/** 选中块与下划线的弹簧参数。 */
const INDICATOR_SPRING = { type: 'spring', stiffness: 520, damping: 40, mass: 0.6 } as const

interface SegmentedTab<T extends string> {
  /** 选项值。 */
  value: T
  /** 选项展示文本。 */
  label: ReactNode
  /** 悬停提示，可选；用于说明按钮上写不下的行为（例如再点一次会怎样）。 */
  title?: string
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
  /** 无障碍名称，可选。 */
  label?: string
}

/**
 * 渲染分段控件。
 *
 * @param props.tabs 选项列表。
 * @param props.value 当前值。
 * @param props.onChange 切换回调。
 * @returns tablist 容器，总高 32px，与按钮、输入框等高。
 */
export function SegmentedTabs<T extends string>({
  tabs,
  value,
  onChange,
  className,
  tabClassName,
  label,
}: SegmentedTabsProps<T>) {
  // LayoutGroup 需要稳定 id，保证多个选项卡实例共存时指示器互不串扰
  const groupId = useId()
  return (
    <LayoutGroup id={groupId}>
      <div
        role="tablist"
        aria-label={label}
        className={cn('inline-flex h-8 items-center gap-0.5 rounded-md bg-nav-active p-0.5', className)}
      >
        {tabs.map((tab) => {
          const active = tab.value === value
          return (
            <button
              key={tab.value}
              type="button"
              role="tab"
              aria-selected={active}
              title={tab.title}
              onClick={() => onChange(tab.value)}
              className={cn(
                'relative h-7 cursor-pointer rounded px-2.5 text-[13px] whitespace-nowrap transition-colors duration-150',
                active ? 'font-medium text-foreground' : 'text-muted-foreground hover:text-foreground',
                tabClassName,
              )}
            >
              {active ? (
                <motion.span
                  layoutId="segmented-tabs-pill"
                  className="absolute inset-0 rounded bg-card shadow-xs"
                  transition={INDICATOR_SPRING}
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

interface UnderlineTabsProps<T extends string> {
  /** 选项列表，按展示顺序排列。 */
  tabs: Array<SegmentedTab<T>>
  /** 当前激活值。 */
  value: T
  /** 切换回调。 */
  onChange: (value: T) => void
  /** 无障碍名称，可选。 */
  label?: string
  /** 追加类名。 */
  className?: string
}

/**
 * 渲染下划线页签。
 *
 * @param props.tabs 选项列表。
 * @param props.value 当前值。
 * @param props.onChange 切换回调。
 * @returns tablist 容器；纵向撑满父容器，放进 48px 标题栏时下划线落在底部分隔线上。
 */
export function UnderlineTabs<T extends string>({ tabs, value, onChange, label, className }: UnderlineTabsProps<T>) {
  const groupId = useId()
  return (
    <LayoutGroup id={groupId}>
      <div role="tablist" aria-label={label} className={cn('flex gap-5 self-stretch', className)}>
        {tabs.map((tab) => {
          const active = tab.value === value
          return (
            <button
              key={tab.value}
              type="button"
              role="tab"
              aria-selected={active}
              title={tab.title}
              onClick={() => onChange(tab.value)}
              className={cn(
                'relative flex cursor-pointer items-center text-[13px] whitespace-nowrap transition-colors duration-150',
                active ? 'font-medium text-foreground' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {tab.label}
              {active ? (
                <motion.span
                  layoutId="underline-tabs-bar"
                  className="absolute inset-x-0 -bottom-px h-0.5 rounded-full bg-primary"
                  transition={INDICATOR_SPRING}
                  aria-hidden="true"
                />
              ) : null}
            </button>
          )
        })}
      </div>
    </LayoutGroup>
  )
}
