/**
 * 登录页：token 校验前的唯一可见界面。
 *
 * 居中卡片承载品牌标识、token 输入与错误提示；画布为白底叠加两团静态蓝色
 * 径向光晕（纯 CSS 渐变，无外部资源，满足 CSP）。登录成功后凭据只保存在
 * HttpOnly Cookie 中，页面不持有 token。
 */
import { LoaderCircle } from 'lucide-react'
import { useState } from 'react'
import type { FormEvent } from 'react'

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
    <div className="relative grid min-h-full place-items-center overflow-hidden bg-background px-4">
      {/* 静态蓝色光晕：营造蓝白氛围但不引入动效开销 */}
      <div
        className="pointer-events-none absolute -top-40 -left-40 size-[34rem] rounded-full opacity-70 dark:opacity-40"
        style={{ background: 'radial-gradient(closest-side, hsl(212 96% 60% / 0.14), transparent 72%)' }}
        aria-hidden="true"
      />
      <div
        className="pointer-events-none absolute -right-40 -bottom-40 size-[30rem] rounded-full opacity-60 dark:opacity-30"
        style={{ background: 'radial-gradient(closest-side, hsl(192 88% 52% / 0.12), transparent 72%)' }}
        aria-hidden="true"
      />
      <div className="absolute top-4 right-4">
        <ThemeSwitch dark={dark} onToggle={toggle} />
      </div>
      <div className="relative w-full max-w-sm rounded-2xl border border-border bg-card p-8 shadow-lifted">
        <div className="flex flex-col items-start gap-4">
          <span
            className="grid size-11 place-items-center rounded-2xl bg-gradient-to-br from-sky-400 to-blue-600 text-xl font-bold text-white shadow-[0_4px_14px_rgb(30_100_220/0.4)]"
            aria-hidden="true"
          >
            Y
          </span>
          <div>
            <p className="text-[11px] font-semibold tracking-[0.18em] text-primary">YUELI / WEBUI</p>
            <h1 className="mt-1 text-xl font-bold tracking-tight">Bot 观察面板</h1>
            <p className="mt-1.5 text-[13px] leading-relaxed text-muted-foreground">
              输入后端启动时公告的 token。登录后凭据只保存在 HttpOnly Cookie 中。
            </p>
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
