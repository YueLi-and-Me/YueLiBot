/**
 * 提供图像供应商共用的 HTTP 状态分类、JSON 请求和有限重试工具。
 *
 * 本模块不决定具体供应商协议；gemini.ts 和 seedream.ts 负责请求路径和 payload，
 * types.ts 定义错误类别，调用方据此决定认证修复、等待或切换供应商。
 */
import { ProviderError, isRetryable, type FailureKind } from './types.ts'

/**
 * 将 HTTP 状态码映射为统一的供应商失败类别。
 *
 * @param status HTTP 响应状态码。
 * @returns {FailureKind} ``401/403`` 映射为鉴权失败，``429`` 映射为配额失败，
 *   ``404`` 映射为模型失败，``5xx`` 映射为网络失败，其他状态映射为未知失败。
 */
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
 * 对可重试的配额或网络错误执行有限次数的指数退避重试。
 *
 * @param fn 单次异步操作；抛出的错误由重试判定器分类。
 * @param opts 重试参数；总次数默认 ``5``，初始退避默认 ``2000`` 毫秒，上限默认
 *   ``60000`` 毫秒，未提供回调时不发送重试通知。
 * @returns {Promise<T>} 首次或重试成功的异步操作结果。
 * @throws {unknown} 首次不可重试、达到最大次数或回调/等待失败时抛出原始错误。
 * @remarks 仅对 ``quota`` 和 ``network`` 类错误重试；鉴权、模型和内容错误立即
 *   传播，避免无效请求消耗完整退避周期。每次退避加入约 ±20% 抖动，减少并发请求同步重试。
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

      // 加入 ±20% 抖动，避免多个请求在同一时刻结束退避并再次触发限流。
      const backoff = Math.min(baseDelayMs * 2 ** i, maxDelayMs)
      const delay = Math.round(backoff * (0.8 + Math.random() * 0.4))
      onRetry?.(i + 1, delay, err)
      await new Promise((r) => setTimeout(r, delay))
    }
  }
  throw lastErr
}

/**
 * 发送 JSON POST 请求，并将传输异常统一转换为网络类 ``ProviderError``。
 *
 * @param url 完整请求 URL，必须能被 ``URL`` 构造器解析。
 * @param headers 额外请求头；函数会覆盖或补充 ``Content-Type: application/json``。
 * @param body 可 JSON 序列化的请求体。
 * @param timeoutMs 请求超时时间，单位为毫秒；默认读取 ``SPRITE_TIMEOUT_MS``，未配置或
 *   非数字时使用 ``300000``。
 * @returns {Promise<{status: number; text: string}>} HTTP 状态码和完整响应正文。
 * @throws {ProviderError} 网络连接失败、URL 无效或请求超时时抛出 ``network`` 类错误；
 *   HTTP 非 2xx 不在此处抛出，由调用方依据状态码解析供应商错误。
 * @remarks 请求体会先序列化为字符串，响应正文会完整载入内存；超时定时器在成功、失败
 *   和异常路径均会清理。
 */
export async function postJson(
  url: string,
  headers: Record<string, string>,
  body: unknown,
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
