/**
 * 应用根组件：装配路由容器、认证上下文与登录门禁。
 *
 * 路由采用浏览器 history 模式，后端已为 `/persons` 与 `/persons/{id}` 回落
 * index.html，因此深链接可直接访问。认证门禁位于路由表之前：会话检查完成前
 * 渲染加载态，未通过认证时整页替换为登录页，通过后才挂载布局壳与业务页面。
 * 被 main.tsx 挂载，是全部页面组件的唯一装配点。
 *
 * 业务页面全部走 React.lazy 动态导入：登录页保持静态引入保证首屏直出，
 * 进入控制台后各页面按需加载，避免单 bundle 超过 500 kB 的构建警告。
 */
import { MotionConfig } from 'motion/react'
import { lazy, Suspense } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router'

import { AppShell } from '@/components/layout/AppShell'
import { Loading, Toaster } from '@/components/ui'
import { LoginScreen } from '@/features/auth/LoginScreen'
import { AuthProvider, useAuth } from '@/hooks/use-auth'

const ObservePage = lazy(() =>
  import('@/features/observe/ObservePage').then((m) => ({ default: m.ObservePage })),
)
const ModelConfigPage = lazy(() =>
  import('@/features/models/ModelConfigPage').then((m) => ({ default: m.ModelConfigPage })),
)
const SettingsConfigPage = lazy(() =>
  import('@/features/settings/SettingsConfigPage').then((m) => ({ default: m.SettingsConfigPage })),
)
const PersonsPage = lazy(() =>
  import('@/features/persons/PersonsPage').then((m) => ({ default: m.PersonsPage })),
)
const PersonDetailPage = lazy(() =>
  import('@/features/persons/PersonDetailPage').then((m) => ({ default: m.PersonDetailPage })),
)
const JargonPage = lazy(() =>
  import('@/features/jargon/JargonPage').then((m) => ({ default: m.JargonPage })),
)
const ExpressionsPage = lazy(() =>
  import('@/features/expressions/ExpressionsPage').then((m) => ({ default: m.ExpressionsPage })),
)
const MemoryGraphPage = lazy(() =>
  import('@/features/memory/MemoryGraphPage').then((m) => ({ default: m.MemoryGraphPage })),
)
const EmojisPage = lazy(() =>
  import('@/features/emojis/EmojisPage').then((m) => ({ default: m.EmojisPage })),
)

/** 页面块加载中的占位：与门禁检查态一致的居中加载样式。 */
function PageFallback() {
  return (
    <div className="grid min-h-full place-items-center bg-background">
      <Loading>正在加载页面…</Loading>
    </div>
  )
}

/**
 * 按认证状态选择渲染登录页还是业务路由表。
 *
 * @returns 加载态、登录页或路由出口。
 * @remarks 未匹配的路径回落到会话观察页：后端只为已知路径回落 index.html，
 * 该分支仅用于开发服务器下的手输地址。Suspense 边界放在 Routes 外层，页面
 * chunk 加载期间整体替换为加载态，避免布局壳先渲染再闪烁。
 */
function AuthGate() {
  const { checking, authenticated } = useAuth()

  if (checking) {
    return (
      <div className="grid min-h-full place-items-center bg-background">
        <Loading>正在检查登录状态…</Loading>
      </div>
    )
  }
  if (!authenticated) return <LoginScreen />

  return (
    <Suspense fallback={<PageFallback />}>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<ObservePage />} />
          <Route path="models" element={<ModelConfigPage />} />
          <Route path="settings" element={<SettingsConfigPage />} />
          <Route path="persons" element={<PersonsPage />} />
          <Route path="persons/:personId" element={<PersonDetailPage />} />
          <Route path="jargon" element={<JargonPage />} />
          <Route path="expressions" element={<ExpressionsPage />} />
          <Route path="memory" element={<MemoryGraphPage />} />
          <Route path="emojis" element={<EmojisPage />} />
          <Route path="*" element={<ObservePage />} />
        </Route>
      </Routes>
    </Suspense>
  )
}

/**
 * 渲染应用根节点。
 *
 * @returns 路由容器包裹的认证上下文与门禁。
 * @remarks 路由容器位于认证上下文之外，使登录页与门禁本身也能使用路由 API；
 * MotionConfig 让全站 motion 动画跟随系统的减弱动态效果偏好；Toaster 挂载
 * 一次即可接收任意位置 `toast.xxx()` 调用。
 */
export function App() {
  return (
    <MotionConfig reducedMotion="user">
      <BrowserRouter>
        <AuthProvider>
          <AuthGate />
          <Toaster />
        </AuthProvider>
      </BrowserRouter>
    </MotionConfig>
  )
}
