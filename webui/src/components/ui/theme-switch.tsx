/**
 * 日月昼夜主题开关组件。
 *
 * 胶囊天空内分四层：底色、云带、星场与日月滑块。亮色态为蓝天白云 + 太阳，
 * 暗色态云带下沉、星场上浮、月盘从右侧推入擦除太阳。状态由 `aria-pressed`
 * 驱动，星点为按半径生成的内联 SVG 路径，尺寸与动画定义在 index.css 的
 * `.theme-switch` 组件层中。被布局侧栏与登录页引用。
 */
import { cn } from './cn'

/** 夜空星点分布：cx/cy 为 SVG 视图坐标，r 为四芒星外接半径。 */
const STARS = [
  { cx: 6, cy: 24, r: 3.4 },
  { cx: 20, cy: 12, r: 2.4 },
  { cx: 34, cy: 26, r: 2.8 },
  { cx: 47, cy: 8, r: 2.2 },
  { cx: 58, cy: 20, r: 3.2 },
  { cx: 72, cy: 10, r: 2.6 },
  { cx: 84, cy: 27, r: 2.2 },
] as const

/**
 * 生成四芒星的 SVG 路径。
 *
 * @param cx 星点中心横坐标。
 * @param cy 星点中心纵坐标。
 * @param r 星芒外接半径。
 * @returns path 元素的 `d` 属性文本。
 * @remarks 四条二次贝塞尔边的控制点取半径的 20%，控制点越靠近中心，星芒收束
 * 越尖锐；取 0 会退化为菱形，取 r 会退化为圆形。
 */
function sparklePath(cx: number, cy: number, r: number): string {
  const control = r * 0.2
  return [
    `M${cx} ${cy - r}`,
    `Q${cx + control} ${cy - control} ${cx + r} ${cy}`,
    `Q${cx + control} ${cy + control} ${cx} ${cy + r}`,
    `Q${cx - control} ${cy + control} ${cx - r} ${cy}`,
    `Q${cx - control} ${cy - control} ${cx} ${cy - r}`,
    'Z',
  ].join(' ')
}

interface ThemeSwitchProps {
  /** 当前是否为暗色主题。 */
  dark: boolean
  /** 点击切换主题的回调。 */
  onToggle: () => void
  /** 追加类名。 */
  className?: string
}

/**
 * 渲染明暗主题切换开关。
 *
 * @param props.dark 暗色主题开关态。
 * @param props.onToggle 切换回调。
 * @returns 胶囊开关按钮。
 */
export function ThemeSwitch({ dark, onToggle, className }: ThemeSwitchProps) {
  return (
    <button
      type="button"
      className={cn('theme-switch', className)}
      aria-pressed={dark}
      aria-label="切换明暗主题"
      title="切换明暗主题"
      onClick={onToggle}
    >
      {/* 云带：单个圆形加一组 box-shadow 副本拼成，样式在 index.css 中定义 */}
      <span className="theme-switch-clouds" aria-hidden="true" />
      <span className="theme-switch-stars" aria-hidden="true">
        <svg viewBox="0 0 90 34" fill="currentColor">
          {STARS.map((star) => (
            <path key={`${star.cx}-${star.cy}`} d={sparklePath(star.cx, star.cy, star.r)} />
          ))}
        </svg>
      </span>
      {/* 光晕层承载滑块并负责左右位移，日盘内部由月盘横向推入完成昼夜互换 */}
      <span className="theme-switch-halo" aria-hidden="true">
        <span className="theme-switch-orb">
          <span className="theme-switch-moon">
            <span className="theme-switch-crater" />
            <span className="theme-switch-crater" />
            <span className="theme-switch-crater" />
          </span>
        </span>
      </span>
    </button>
  )
}
