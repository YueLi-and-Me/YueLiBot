import { join, normalize, sep } from 'node:path'
import { pathToFileURL } from 'node:url'
import { net, protocol } from 'electron'

/**
 * 生产环境用自定义 `app://` 协议加载渲染层。
 *
 * 直接 loadFile 走 `file://` 会有两个致命问题：
 *   · `fetch('/character/xxx')` 会解析成 `file:///character/xxx` —— 绝对路径失去意义
 *   · file:// 源下 fetch 本身就被禁止，manifest 根本读不出来
 *
 * 注册成 standard + secure + supportFetchAPI 后，取资源行为与 http 一致，
 * dev 和生产走同一套路径，不用在渲染层里写 if (isDev)。
 */
const SCHEME = 'app'
const HOST = 'bundle'

/** 必须在 app.whenReady 之前调用。 */
export function registerAppScheme(): void {
  protocol.registerSchemesAsPrivileged([
    {
      scheme: SCHEME,
      privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true },
    },
  ])
}

/** 在 app.whenReady 之后调用，root 为渲染层产物目录。 */
export function serveAppScheme(root: string): void {
  const base = normalize(root)

  protocol.handle(SCHEME, async (request) => {
    const { pathname } = new URL(request.url)
    const rel = decodeURIComponent(pathname).replace(/^\/+/, '')
    const target = normalize(join(base, rel))

    // 路径穿越防护：解析后必须仍在产物目录内
    if (target !== base && !target.startsWith(base + sep)) {
      return new Response('forbidden', { status: 403 })
    }

    return net.fetch(pathToFileURL(target).toString())
  })
}

export const APP_INDEX_URL = `${SCHEME}://${HOST}/index.html`
export const APP_CAPTURE_URL = `${SCHEME}://${HOST}/capture.html`
export const APP_DIARY_URL = `${SCHEME}://${HOST}/diary.html`
export const APP_OBSERVABILITY_URL = `${SCHEME}://${HOST}/observability.html`
export const APP_SETTINGS_URL = `${SCHEME}://${HOST}/settings.html`
