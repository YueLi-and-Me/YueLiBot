/**
 * 页面头部组件： eyebrow 标识、标题、副标题与右侧工具区。
 *
 * 每个业务页面（会话观察/人物画像）的顶部统一由本组件渲染，工具区放置
 * 会话流选择、刷新控制等页面级操作。
 */
import type { ReactNode } from 'react'

interface PageHeaderProps {
  /** 顶部小号大写标识文本，如 `YUELI / WEBUI`。 */
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
        <p className="text-[11px] font-semibold tracking-[0.18em] text-primary">{eyebrow}</p>
        <h1 className="mt-1 text-2xl font-bold tracking-tight">{title}</h1>
        {subtitle ? <p className="mt-1 text-[13px] text-muted-foreground">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex flex-none flex-wrap items-center gap-2.5">{actions}</div> : null}
    </header>
  )
}
