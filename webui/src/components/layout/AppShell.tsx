/**
 * 应用布局壳：浮动侧栏 + 极光画布上的内容区。
 *
 * 桌面端为「浮动侧栏 + 主区」横向布局；窄屏侧栏隐藏，退化为顶部导航条。背景
 * 由 body 的极光渐变画布承担，本层保持透明；主区独立纵向滚动，路由出口由
 * react-router 的 Outlet 渲染。路由切换时出口容器按 pathname 重挂载并播放
 * page-in 入场，避免页面瞬间硬切。
 */
import { Outlet, useLocation } from 'react-router'

import { MobileTopbar, Sidebar } from './Sidebar'

/**
 * 渲染应用整体布局。
 *
 * @returns 布局壳元素，高度撑满视口并禁止整页滚动。
 */
export function AppShell() {
  const { pathname } = useLocation()

  return (
    <div className="flex h-full overflow-hidden text-foreground">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <MobileTopbar />
        <main className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          <div key={pathname} className="min-h-full animate-page-in">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
