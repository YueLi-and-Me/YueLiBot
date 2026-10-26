/**
 * 应用根组件：装配路由容器、认证上下文与登录门禁。
 *
 * 路由采用浏览器 history 模式，后端已为 `/persons` 与 `/persons/{id}` 回落
 * index.html，因此深链接可直接访问。两条人物路由渲染同一页（列表加详情），
 * 选中项由路由参数决定。认证门禁位于路由表之前：会话检查完成前
 * 渲染加载态，未通过认证时整页替换为登录页，通过后才挂载布局壳与业务页面。
 * 被 main.tsx 挂载，是全部页面组件的唯一装配点。
 *
 * 业务页面全部走 React.lazy 动态导入：登录页保持静态引入保证首屏直出，
 * 进入控制台后各页面按需加载，避免单 bundle 超过 500 kB 的构建警告。页面块
 * 加载期间的占位放在布局壳里（AppShell），侧栏不随页面切换闪烁。
 */
import { MotionConfig } from 'motion/react'
import { lazy } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router'

import { AppShell } from '@/components/layout/AppShell'
import { Loading, Toaster } from '@/components/ui'
import { LoginScreen } from '@/features/auth/LoginScreen'
import { AuthProvider, useAuth } from '@/hooks/use-auth'

const ObservePage = lazy(() =>
  import('@/features/observe/ObservePage').then((m) => ({ default: m.ObservePage })),
)
const UsagePage = lazy(() =>
  import('@/features/usage/UsagePage').then((m) => ({ default: m.UsagePage })),
)
const ModelConfigPage = lazy(() =>
  import('@/features/models/ModelConfigPage').then((m) => ({ default: m.ModelConfigPage })),
)
const SettingsConfigPage = lazy(() =>
  import('@/features/settings/SettingsConfigPage').then((m) => ({ default: m.SettingsConfigPage })),
)
const ToolsConfigPage = lazy(() =>
  import('@/features/tools/ToolsConfigPage').then((m) => ({ default: m.ToolsConfigPage })),
)
const DeveloperCommandsPage = lazy(() =>
  import('@/features/developer/DeveloperCommandsPage').then((m) => ({ default: m.DeveloperCommandsPage })),
)
const PersonsPage = lazy(() =>
  import('@/features/persons/PersonsPage').then((m) => ({ default: m.PersonsPage })),
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
const RetrievalTuningPage = lazy(() =>
  import('@/features/memory/RetrievalTuningPage').then((m) => ({ default: m.RetrievalTuningPage })),
)
const ImportCenterPage = lazy(() =>
  import('@/features/memory/ImportCenterPage').then((m) => ({ default: m.ImportCenterPage })),
)
const MemoryManagePage = lazy(() =>
  import('@/features/memory/MemoryManagePage').then((m) => ({ default: m.MemoryManagePage })),
)
const EmojisPage = lazy(() =>
  import('@/features/emojis/EmojisPage').then((m) => ({ default: m.EmojisPage })),
)

/**
 * 按认证状态选择渲染登录页还是业务路由表。
 *
 * @returns 加载态、登录页或路由出口。
 * @remarks 未匹配的路径回落到会话观察页：后端只为已知路径回落 index.html，
 * 该分支仅用于开发服务器下的手输地址。
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
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<ObservePage />} />
        <Route path="usage" element={<UsagePage />} />
        <Route path="models" element={<ModelConfigPage />} />
        <Route path="tools" element={<ToolsConfigPage />} />
        <Route path="settings" element={<SettingsConfigPage />} />
        <Route path="developer/commands" element={<DeveloperCommandsPage />} />
        <Route path="persons" element={<PersonsPage />} />
        <Route path="persons/:personId" element={<PersonsPage />} />
        <Route path="jargon" element={<JargonPage />} />
        <Route path="expressions" element={<ExpressionsPage />} />
        <Route path="memory" element={<MemoryGraphPage />} />
        <Route path="memory/tuning" element={<RetrievalTuningPage />} />
        <Route path="memory/import" element={<ImportCenterPage />} />
        <Route path="memory/manage" element={<MemoryManagePage />} />
        <Route path="emojis" element={<EmojisPage />} />
        <Route path="*" element={<ObservePage />} />
      </Route>
    </Routes>
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
