/**
 * 登录页：token 校验前的唯一可见界面。
 *
 * 银白框架色的画布上居中放一块登录卡片，卡片里是月璃的头像、token 输入与错误
 * 提示，视觉与控制台的侧栏、面板一致。登录成功后凭据只保存在 HttpOnly Cookie
 * 中，页面不持有 token。
 */
import { LoaderCircle } from 'lucide-react'
import { useState } from 'react'
import type { FormEvent } from 'react'

import { BrandMark } from '@/components/layout/BrandMark'
import { Button, Input, ThemeSwitch } from '@/components/ui'
import { useAuth } from '@/hooks/use-auth'
import { useTheme } from '@/hooks/use-theme'

/**
 * 渲染登录页。
 *
 * @returns 登录页全屏容器。
 * @remarks 提交期间禁用按钮并显示加载图标；错误文本由认证上下文提供
 * （登录失败、会话失效、后端不可达等）。
 */
export function LoginScreen() {
  const { login, loginMessage } = useAuth()
  const { dark, toggle } = useTheme()
  const [token, setToken] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [localError, setLocalError] = useState('')

  const onSubmit = (event: FormEvent) => {
    event.preventDefault()
    setLocalError('')
    setSubmitting(true)
    void login(token).then((error) => {
      setSubmitting(false)
      setToken('')
      if (error) setLocalError(error)
    })
  }

  const message = localError || loginMessage

  return (
    <div className="relative grid min-h-full place-items-center bg-chrome px-4">
      <div className="absolute top-4 right-4">
        <ThemeSwitch dark={dark} onToggle={toggle} />
      </div>
      <div className="w-full max-w-sm animate-scale-in rounded-xl border border-border bg-card p-8 shadow-dialog">
        <div className="flex flex-col gap-6">
          <div className="flex flex-col items-center gap-3 text-center">
            <BrandMark className="size-16" />
            <div>
              <h1 className="text-xl font-semibold">月璃</h1>
              <p className="mt-0.5 text-[13px] text-muted-foreground">管理控制台</p>
            </div>
          </div>
          <form onSubmit={onSubmit} className="flex w-full flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <label htmlFor="token" className="text-xs font-medium text-muted-foreground">
                后端 token
              </label>
              <Input
                id="token"
                name="token"
                type="password"
                autoComplete="off"
                required
                value={token}
                onChange={(event) => setToken(event.target.value)}
              />
            </div>
            <Button type="submit" disabled={submitting} className="w-full">
              {submitting ? <LoaderCircle className="size-4 animate-spin-slow" aria-hidden="true" /> : null}
              登录
            </Button>
            {message ? (
              <p role="alert" aria-live="polite" className="text-[13px] text-destructive">
                {message}
              </p>
            ) : null}
          </form>
        </div>
      </div>
    </div>
  )
}
