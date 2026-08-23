/**
 * 登录页：token 校验前的唯一可见界面。
 *
 * 居中卡片承载品牌标识、token 输入与错误提示；画布叠加两团静态樱粉/天蓝
 * 径向光晕（纯 CSS 渐变，无外部资源，满足 CSP）。登录成功后凭据只保存在
 * HttpOnly Cookie 中，页面不持有 token。
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
    <div className="relative grid min-h-full place-items-center overflow-hidden bg-background px-4">
      {/* 静态樱粉/天蓝双色光晕：呼应品牌渐变但不引入动效开销 */}
      <div
        className="pointer-events-none absolute -top-40 -left-40 size-[34rem] rounded-full opacity-70 dark:opacity-40"
        style={{ background: 'radial-gradient(closest-side, hsl(340 92% 72% / 0.16), transparent 72%)' }}
        aria-hidden="true"
      />
      <div
        className="pointer-events-none absolute -right-40 -bottom-40 size-[30rem] rounded-full opacity-60 dark:opacity-30"
        style={{ background: 'radial-gradient(closest-side, hsl(205 95% 66% / 0.14), transparent 72%)' }}
        aria-hidden="true"
      />
      <div className="absolute top-4 right-4">
        <ThemeSwitch dark={dark} onToggle={toggle} />
      </div>
      <div className="relative w-full max-w-sm animate-scale-in rounded-2xl border-[1.5px] border-ink bg-card p-8 shadow-lifted">
        <div className="flex flex-col gap-6">
          <div className="flex flex-col items-center gap-4 text-center">
            <BrandMark className="size-12 rounded-2xl" />
            <div>
              <p className="text-[11px] font-semibold tracking-[0.18em] text-primary-strong">YUELI · CONSOLE</p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight">月璃</h1>
              <p className="mt-1.5 text-sm text-muted-foreground">管理控制台</p>
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
