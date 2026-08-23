/**
 * 品牌标识：樱粉 → 天蓝渐变圆角块 + 白色水母图形。
 *
 * 渐变取自 index.css 的 `--brand-gradient` 令牌，是界面中唯一常驻的渐变元素；
 * 侧栏品牌区、窄屏顶栏与登录页共用本组件，保证品牌一致性。尺寸与圆角由
 * 调用方通过 className 控制（如 `size-9 rounded-xl`）。
 */
import { cn } from '@/components/ui/cn'

interface BrandMarkProps {
  /** 尺寸与圆角类名，如 `size-9 rounded-xl`。 */
  className?: string
}

/**
 * 渲染品牌标识块。
 *
 * @param props.className 尺寸与圆角类名。
 * @returns 渐变标识元素，对读屏隐藏。
 */
export function BrandMark({ className }: BrandMarkProps) {
  return (
    <span
      className={cn('grid flex-none place-items-center text-white shadow-card', className)}
      style={{ background: 'var(--brand-gradient)' }}
      aria-hidden="true"
    >
      <svg viewBox="0 0 24 24" fill="none" className="h-[60%] w-[60%]">
        {/* 水母伞盖 */}
        <path
          d="M12 4.5c-4.4 0-7.5 3.3-7.5 7.1 0 .9.8 1.6 1.7 1.6h11.6c.9 0 1.7-.7 1.7-1.6 0-3.8-3.1-7.1-7.5-7.1Z"
          fill="currentColor"
          opacity="0.95"
        />
        {/* 三条触手 */}
        <path
          d="M8.2 15.2c-.66 1.3-.66 2.6 0 3.9M12 15.5V19M15.8 15.2c.66 1.3.66 2.6 0 3.9"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
        />
      </svg>
    </span>
  )
}
