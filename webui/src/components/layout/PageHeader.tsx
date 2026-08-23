/**
 * 页面头部组件： eyebrow 标识、标题、副标题与右侧工具区。
 *
 * 每个业务页面（会话观察/人物画像）的顶部统一由本组件渲染，工具区放置
 * 会话流选择、刷新控制等页面级操作。排版层级：11px 加宽字距 eyebrow →
 * 22px 半粗标题 → 13px muted 副标题。
 */
import type { ReactNode } from 'react'

interface PageHeaderProps {
  /** 顶部小号大写标识文本，如 `YUELI · CONSOLE`。 */
  eyebrow: string
  /** 页面主标题。 */
  title: string
  /** 标题下的副标题说明。 */
  subtitle?: string
  /** 右侧工具区节点，可选。 */
  actions?: ReactNode
}

/**
 * 渲染页面头部。
 *
 * @param props.eyebrow 标识文本。
 * @param props.title 主标题。
 * @param props.subtitle 副标题。
 * @param props.actions 工具区节点。
 * @returns header 元素；工具区与标题组在窄屏下纵向堆叠。
 */
export function PageHeader({ eyebrow, title, subtitle, actions }: PageHeaderProps) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-4">
      <div className="min-w-0">
        <p
          className="w-fit bg-clip-text text-[11px] font-bold tracking-[0.16em] text-transparent"
          style={{ backgroundImage: 'var(--brand-gradient)' }}
        >
          {eyebrow}
        </p>
        <h1 className="mt-1.5 text-[22px] leading-8 font-semibold tracking-tight">{title}</h1>
        {subtitle ? <p className="mt-1 text-[13px] text-muted-foreground">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex flex-none flex-wrap items-center gap-2.5">{actions}</div> : null}
    </header>
  )
}
