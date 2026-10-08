/**
 * 人物头像占位：取名字的第一个字形成圆形字标，人物列表与画像面板共用。
 *
 * 后端不保存人物头像，这里只做可辨识的占位；英文名取前两个字母，中文名取首字。
 */
import { cn } from '@/components/ui'

interface PersonAvatarProps {
  /** 人物显示名。 */
  name: string
  /** 是否为选中态：主色实底白字；否则为灰底弱色字。 */
  selected?: boolean
  /** 尺寸与字号类名，如 `size-8 text-xs`。 */
  className?: string
}

/**
 * 计算头像上显示的字标。
 *
 * @param name 显示名。
 * @returns 中文取首字，拉丁字母取前两个字母，空名返回「?」。
 */
function initialOf(name: string): string {
  const trimmed = name.trim()
  if (!trimmed) return '?'
  const chars = Array.from(trimmed)
  const first = chars[0] ?? '?'
  return /[a-z]/i.test(first) ? chars.slice(0, 2).join('') : first
}

/**
 * 渲染人物字标头像。
 *
 * @param props.name 显示名。
 * @param props.selected 选中态。
 * @param props.className 尺寸类名。
 * @returns 圆形字标，对读屏隐藏（旁边总有名字）。
 */
export function PersonAvatar({ name, selected = false, className }: PersonAvatarProps) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'grid flex-none place-items-center rounded-full font-semibold select-none',
        selected ? 'bg-primary text-primary-foreground' : 'bg-nav-active text-muted-foreground',
        className,
      )}
    >
      {initialOf(name)}
    </span>
  )
}
