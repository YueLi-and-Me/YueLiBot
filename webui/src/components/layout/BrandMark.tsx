/**
 * 品牌标识：月璃本人的大头照，圆形裁切。
 *
 * 图源与文档站 logo 同一张（docs/images/logo.png 的副本），侧栏品牌区、窄屏抽屉
 * 与登录页共用本组件。尺寸由调用方通过 className 控制（如 `size-7`）。
 */
import avatarUrl from '@/assets/yueli-avatar.png'
import { cn } from '@/components/ui/cn'

interface BrandMarkProps {
  /** 尺寸类名，如 `size-7`。 */
  className?: string
}

/**
 * 渲染品牌头像。
 *
 * @param props.className 尺寸类名。
 * @returns 圆形头像图片；旁边总有「月璃」字样，因此对读屏隐藏。
 */
export function BrandMark({ className }: BrandMarkProps) {
  return (
    <img
      src={avatarUrl}
      alt=""
      aria-hidden="true"
      draggable={false}
      className={cn('flex-none rounded-full bg-muted object-cover ring-1 ring-border', className)}
    />
  )
}
