/**
 * 应用布局壳：左侧导航栏 + 右侧工作区。
 *
 * 桌面端侧栏常驻（可收起为图标栏），工作区由各页面的 PageFrame 自行渲染顶栏与
 * 面板；窄屏侧栏隐藏，改由页面顶栏左侧的菜单按钮唤出导航抽屉。抽屉的开关状态
 * 放在本组件的上下文里，供 PageFrame 的菜单按钮读写。
 */
import { createContext, Suspense, useContext, useEffect, useMemo, useState } from 'react'
import { Outlet, useLocation } from 'react-router'

import { Loading } from '@/components/ui'

import { PageErrorBoundary } from './PageErrorBoundary'
import { NavDrawer, Sidebar } from './Sidebar'

interface NavDrawerState {
  /** 窄屏导航抽屉是否打开。 */
  open: boolean
  /** 打开或关闭抽屉。 */
  setOpen: (open: boolean) => void
}

const NavDrawerContext = createContext<NavDrawerState | null>(null)

/**
 * 读取窄屏导航抽屉的开关状态。
 *
 * @returns 抽屉状态与开关函数。
 * @throws Error 在 AppShell 之外调用时抛出，说明组件被放错了位置。
 */
export function useNavDrawer(): NavDrawerState {
  const state = useContext(NavDrawerContext)
  if (!state) throw new Error('useNavDrawer 必须在 AppShell 内使用')
  return state
}

/**
 * 渲染应用整体布局。
 *
 * @returns 布局壳元素，高度撑满视口；页面不整体滚动，滚动发生在各面板内部。
 * @remarks 路由切换时工作区按 pathname 重挂载并淡入。人物详情等子路由与列表同属
 * 一页，切换选中项不应整页闪烁，因此重挂载的键取路径第一段。
 */
export function AppShell() {
  const { pathname } = useLocation()
  const [open, setOpen] = useState(false)
  const drawer = useMemo(() => ({ open, setOpen }), [open])
  const section = pathname.split('/')[1] ?? ''

  useEffect(() => {
    setOpen(false)
  }, [pathname])

  return (
    <NavDrawerContext.Provider value={drawer}>
      <div className="flex h-full overflow-hidden bg-background text-foreground">
        <Sidebar />
        <div key={section} className="flex min-w-0 flex-1 animate-page-in flex-col">
          {/* 页面块按需加载；占位只替换工作区，侧栏保持不动。页面渲染出错时只在
              工作区显示错误原文，不让整个应用白屏。 */}
          <PageErrorBoundary resetKey={pathname}>
            <Suspense
              fallback={
                <div className="grid flex-1 place-items-center">
                  <Loading>正在加载页面…</Loading>
                </div>
              }
            >
              <Outlet />
            </Suspense>
          </PageErrorBoundary>
        </div>
        <NavDrawer open={open} onClose={() => setOpen(false)} />
      </div>
    </NavDrawerContext.Provider>
  )
}
