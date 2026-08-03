import { ProviderError, isRetryable, type FailureKind } from './types.ts'

/** HTTP 状态码 → 失败分类。两个 provider 共用。 */
export function classifyStatus(status: number): FailureKind {
  if (status === 401 || status === 403) return 'auth'
  if (status === 429) return 'quota'
  if (status === 404) return 'model'
  if (status >= 500) return 'network' // 服务端 5xx 当作可重试的抖动
  return 'unknown'
}

export interface RetryOptions {
  /** 总尝试次数（含首次）。 */
  attempts?: number
  /** 首次退避毫秒数，之后指数增长。 */
  baseDelayMs?: number
  /** 退避上限，避免 429 把等待拖到几十分钟。 */
  maxDelayMs?: number
  /** 每次重试前回调，用于打日志。 */
  onRetry?: (attempt: number, delayMs: number, err: unknown) => void
}

/**
 * 指数退避重试。只对 quota / network 生效——鉴权错误立刻抛出，
 * 免得用户对着一个填错的 Key 干等两分钟。
 */
export async function withRetry<T>(fn: () => Promise<T>, opts: RetryOptions = {}): Promise<T> {
  const { attempts = 5, baseDelayMs = 2_000, maxDelayMs = 60_000, onRetry } = opts

  let lastErr: unknown
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn()
    } catch (err) {
      lastErr = err
      if (!isRetryable(err) || i === attempts - 1) throw err

      // 加 ±20% 抖动，避免多个请求同时醒来又一起撞限
      const backoff = Math.min(baseDelayMs * 2 ** i, maxDelayMs)
      const delay = Math.round(backoff * (0.8 + Math.random() * 0.4))
      onRetry?.(i + 1, delay, err)
      await new Promise((r) => setTimeout(r, delay))
    }
  }
  throw lastErr
}

/** fetch 包一层，把连不上网络的异常也归进 ProviderError，让上层只处理一种错误类型。 */
export async function postJson(
  url: string,
  headers: Record<string, string>,
  body: unknown,
  // Seedream 5.0 Pro 单张实测约 65s，复杂角色更久。
  // 客户端超时是白付钱（服务端照样出图并计费），宁可等
  timeoutMs = Number(process.env.SPRITE_TIMEOUT_MS) || 300_000,
): Promise<{ status: number; text: string }> {
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), timeoutMs)
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers },
      body: JSON.stringify(body),
      signal: ctl.signal,
    })
    return { status: res.status, text: await res.text() }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    const hint = ctl.signal.aborted
      ? `请求超时（${timeoutMs / 1000}s）`
      : `无法连接 ${new URL(url).host}`
    throw new ProviderError('network', hint, msg)
  } finally {
    clearTimeout(timer)
  }
}
