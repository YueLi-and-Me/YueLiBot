/**
 * 面板认证状态的全局上下文。
 *
 * 挂载时检查 `/auth/session`；登录、登出与各数据请求抛出的 401 统一汇聚到
 * 本上下文处理：切换到登录页并携带提示文本。被 app/App.tsx 提供、被全部
 * 数据 hooks 与页面组件消费。
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import { apiFetch, apiMutate, UnauthorizedError } from '@/lib/api'

/** 认证上下文暴露的状态与方法。 */
interface AuthContextValue {
  /** 会话检查是否已完成（未完成时渲染加载态）。 */
  checking: boolean
  /** 当前是否已通过认证。 */
  authenticated: boolean
  /** 登录页需要展示的错误/提示文本。 */
  loginMessage: string
  /**
   * 提交 token 登录。
   *
   * @param token 后端启动时公告的访问令牌。
   * @returns 登录失败时返回错误文本；成功返回 `null`。
   */
  login: (token: string) => Promise<string | null>
  /** 登出并返回登录页。 */
  logout: () => Promise<void>
  /**
   * 处理任意数据请求产生的未授权错误。
   *
   * @param error 捕获的未知错误；仅 UnauthorizedError 会触发登录页切换。
   */
  handleUnauthorized: (error: unknown) => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

/**
 * 提供认证上下文的容器组件。
 *
 * @param props.children 子树节点。
 * @returns 上下文 Provider。
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [checking, setChecking] = useState(true)
  const [authenticated, setAuthenticated] = useState(false)
  const [loginMessage, setLoginMessage] = useState('')

  useEffect(() => {
    let cancelled = false
    const autoLogin = async () => {
      try {
        const state = await apiFetch<{ authenticated: boolean }>('/auth/session')
        if (!cancelled) setAuthenticated(state.authenticated)
        if (state.authenticated) return
      } catch {
        if (!cancelled) setLoginMessage('连接不到后端，请确认服务已启动。')
        return
      }
      try {
        await apiMutate('/auth/auto', 'POST')
        if (!cancelled) setAuthenticated(true)
      } catch {
        if (!cancelled) setLoginMessage('自动登录不可用，请输入后端 token。')
      }
    }
    void autoLogin().finally(() => {
      if (!cancelled) setChecking(false)
    })
    return () => {
      cancelled = true
    }
  }, [])

  const login = useCallback(async (token: string) => {
    try {
      await apiMutate('/auth/login', 'POST', { token })
    } catch (error) {
      return error instanceof UnauthorizedError
        ? 'token 不正确，请重新输入。'
        : '登录失败，请查看后端日志。'
    }
    setLoginMessage('')
    setAuthenticated(true)
    return null
  }, [])

  const logout = useCallback(async () => {
    // 401 视为已登出：会话本就失效，只需切回登录页。
    await apiMutate('/auth/logout', 'POST').catch((error: unknown) => {
      if (!(error instanceof UnauthorizedError)) throw error
    })
    setAuthenticated(false)
    setLoginMessage('已安全登出。')
  }, [])

  const handleUnauthorized = useCallback((error: unknown) => {
    if (error instanceof UnauthorizedError) {
      setAuthenticated(false)
      setLoginMessage('登录已失效，请重新输入 token。')
    }
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ checking, authenticated, loginMessage, login, logout, handleUnauthorized }),
    [checking, authenticated, loginMessage, login, logout, handleUnauthorized],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

/**
 * 读取认证上下文。
 *
 * @returns 认证状态与方法。
 * @throws Error 在 AuthProvider 之外调用时抛出。
 */
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth 必须在 AuthProvider 内使用')
  return context
}
