/**
 * 观察面板 HTTP 接口的统一封装。
 *
 * 所有请求携带同源凭据（HttpOnly Cookie 会话）；401 统一抛出 UnauthorizedError，
 * 由认证上下文转换为登录页切换；404 抛出 NotFoundError，由调用方转换为「资源
 * 不存在」的页面状态；其他非成功状态抛出带后端 detail 的错误。
 * 被 hooks 与 features 下的数据加载逻辑依赖。
 */

/** 会话失效或未登录时抛出的错误类型，用于全局 401 识别。 */
export class UnauthorizedError extends Error {
  constructor() {
    super('UNAUTHORIZED')
    this.name = 'UnauthorizedError'
  }
}

/**
 * 请求的资源不存在时抛出的错误类型。
 *
 * 与通用错误分开，是因为 404 在人物画像等按 ID 寻址的页面上属于正常的用户
 * 路径（手输了不存在的 ID），需要展示专门的提示而非通用的请求失败文本。
 */
export class NotFoundError extends Error {
  constructor() {
    super('NOT_FOUND')
    this.name = 'NotFoundError'
  }
}

/**
 * 从错误响应体中提取后端返回的 detail 文本。
 *
 * @param response 非成功状态的 Response。
 * @returns 后端 detail 或 `HTTP <status>` 兜底文本。
 */
async function errorDetail(response: Response): Promise<string> {
  const payload = await response.json().catch(() => ({})) as { detail?: unknown }
  return typeof payload.detail === 'string' && payload.detail.trim()
    ? payload.detail
    : `HTTP ${response.status}`
}

/**
 * 发起同源 JSON 请求并按约定处理错误。
 *
 * @param path 接口路径，以 `/` 开头。
 * @param init 可选的请求配置；凭据固定为 `same-origin`。
 * @returns 解析后的 JSON 响应体。
 * @throws UnauthorizedError 响应状态为 401 时抛出。
 * @throws NotFoundError 响应状态为 404 时抛出。
 * @throws Error 其他非成功状态抛出带后端 detail 的错误。
 */
export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', ...init })
  if (response.status === 401) throw new UnauthorizedError()
  if (response.status === 404) throw new NotFoundError()
  if (!response.ok) throw new Error(await errorDetail(response))
  return await response.json() as T
}

/**
 * 发起带 JSON 请求体的同源请求。
 *
 * @param path 接口路径。
 * @param method HTTP 方法，限定为写操作。
 * @param body 待序列化的请求体；`undefined` 时不携带请求体。
 * @returns 解析后的 JSON 响应体。
 * @throws 与 {@link apiFetch} 相同。
 */
export async function apiMutate<T>(path: string, method: 'POST' | 'PUT' | 'DELETE', body?: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}
