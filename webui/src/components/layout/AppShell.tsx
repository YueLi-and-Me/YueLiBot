/**
 * 应用布局壳：深藏青侧栏 + 白色内容区的整体框架。
 *
 * 桌面端为「侧栏 + 主区」横向布局；窄屏侧栏隐藏，退化为顶部导航条。主区
 * 独立纵向滚动，路由出口由 react-router 的 Outlet 渲染。
 */
import { Outlet } from 'react-router'

import { MobileTopbar, Sidebar } from './Sidebar'

/**
 * 渲染应用整体布局。
 *
 * @returns 布局壳元素，高度撑满视口并禁止整页滚动。
 */
export function AppShell() {
  return (
    <div className="flex h-full overflow-hidden bg-background text-foreground">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <MobileTopbar />
        <main className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
