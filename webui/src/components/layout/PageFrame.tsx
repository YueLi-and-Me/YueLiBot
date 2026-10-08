/**
 * 页面外框：工作区顶栏 + 正文区域。
 *
 * 顶栏高 56px，左侧是由路由推导的位置路径（分组 › 页面 › 子项），右侧是本页的
 * 操作按钮；窄屏时左端多一个唤出导航抽屉的菜单按钮。正文区域不滚动，由页面在
 * 其中放入面板（panes.tsx），滚动发生在各面板内部，因此顶栏始终可见、页面操作
 * 随时点得到。每个业务页面的最外层都是本组件。
 */
import { ChevronRight, Info, Menu } from 'lucide-react'
import { useEffect, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router'

import { useNavDrawer } from './AppShell'
import { findActiveNav } from './nav'

interface PageFrameProps {
  /** 位置路径的末段，例如人物详情页的人名；不传时路径止于页面名。 */
  detail?: ReactNode
  /** 页面用途的一句话说明，以页面名旁的提示图标呈现，悬停可见。 */
  hint?: string
  /** 顶栏右侧的页面操作。 */
  actions?: ReactNode
  /** 正文：通常是 Panes，或 Toolbar + ScrollArea 的纵向组合。 */
  children: ReactNode
}

/**
 * 渲染页面外框。
 *
 * @param props.detail 位置路径末段。
 * @param props.hint 页面用途说明。
 * @param props.actions 页面操作。
 * @param props.children 正文。
 * @returns 撑满工作区的纵向容器。
 * @remarks 页面名取自 nav.ts，保证侧栏、位置路径与浏览器标题三处叫法一致；
 * 有 detail 时页面名变为可点击的返回链接。
 */
export function PageFrame({ detail, hint, actions, children }: PageFrameProps) {
  const { pathname } = useLocation()
  const { group, item } = findActiveNav(pathname)
  const { setOpen } = useNavDrawer()

  useEffect(() => {
    document.title = `${item.label} · 月璃`
  }, [item.label])

  return (
    <div data-page-frame className="flex h-full min-h-0 flex-col">
      <header className="flex min-h-14 flex-none flex-wrap items-center gap-x-3 gap-y-2 border-b border-border px-4 py-2 sm:px-6">
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="打开导航"
          className="-ml-1.5 inline-flex size-8 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground lg:hidden"
        >
          <Menu className="size-[18px]" aria-hidden="true" />
        </button>
        <nav aria-label="位置" className="flex min-w-0 items-center gap-1.5 text-[13px]">
          <span className="hidden text-subtle-foreground sm:inline">{group.label}</span>
          <ChevronRight className="hidden size-3.5 flex-none text-subtle-foreground sm:block" aria-hidden="true" />
          {detail ? (
            <>
              <Link to={item.to} className="truncate text-muted-foreground transition-colors hover:text-foreground">
                {item.label}
              </Link>
              <ChevronRight className="size-3.5 flex-none text-subtle-foreground" aria-hidden="true" />
              <h1 className="truncate font-semibold">{detail}</h1>
            </>
          ) : (
            <h1 className="truncate font-semibold">{item.label}</h1>
          )}
          {hint ? (
            <span title={hint} className="inline-flex flex-none cursor-help text-subtle-foreground hover:text-muted-foreground">
              <Info className="size-3.5" aria-hidden="true" />
              <span className="sr-only">{hint}</span>
            </span>
          ) : null}
        </nav>
        {actions ? <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div> : null}
      </header>
      <div className="flex min-h-0 flex-1 flex-col">{children}</div>
    </div>
  )
}
