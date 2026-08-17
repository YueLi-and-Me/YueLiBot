/**
 * 应用根组件：装配路由容器、认证上下文与登录门禁。
 *
 * 路由采用浏览器 history 模式，后端已为 `/persons` 与 `/persons/{id}` 回落
 * index.html，因此深链接可直接访问。认证门禁位于路由表之前：会话检查完成前
 * 渲染加载态，未通过认证时整页替换为登录页，通过后才挂载布局壳与业务页面。
 * 被 main.tsx 挂载，是全部页面组件的唯一装配点。
 */
import { BrowserRouter, Route, Routes } from 'react-router'

import { AppShell } from '@/components/layout/AppShell'
import { Loading } from '@/components/ui'
import { LoginScreen } from '@/features/auth/LoginScreen'
import { ModelConfigPage } from '@/features/models/ModelConfigPage'
import { ObservePage } from '@/features/observe/ObservePage'
import { PersonDetailPage } from '@/features/persons/PersonDetailPage'
import { PersonsPage } from '@/features/persons/PersonsPage'
import { SettingsConfigPage } from '@/features/settings/SettingsConfigPage'
import { AuthProvider, useAuth } from '@/hooks/use-auth'

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
        <Route path="models" element={<ModelConfigPage />} />
        <Route path="settings" element={<SettingsConfigPage />} />
        <Route path="persons" element={<PersonsPage />} />
        <Route path="persons/:personId" element={<PersonDetailPage />} />
        <Route path="*" element={<ObservePage />} />
      </Route>
    </Routes>
  )
}

/**
 * 渲染应用根节点。
 *
 * @returns 路由容器包裹的认证上下文与门禁。
 * @remarks 路由容器位于认证上下文之外，使登录页与门禁本身也能使用路由 API。
 */
export function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AuthGate />
      </AuthProvider>
    </BrowserRouter>
  )
}
