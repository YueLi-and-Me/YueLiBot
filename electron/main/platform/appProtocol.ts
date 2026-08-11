/**
 * 注册生产环境使用的 app:// 自定义协议，并将请求映射到渲染资源目录。
 *
 * 该模块只处理 Electron 协议解析和文件 URL 安全校验；窗口创建由同目录的窗口
 * 模块负责，渲染层通过主进程加载本模块暴露的协议。
 */
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

/**
 * 在 ``app.whenReady`` 之前声明生产环境的自定义协议权限。
 *
 * @returns {void} 无返回值；协议声明完成后由 Electron 在应用就绪时生效。
 * @throws {Error} Electron 拒绝注册协议权限时由运行时抛出。
 * @remarks 必须在 ``app.whenReady`` 之前调用；该函数只声明协议，不注册请求处理器。
 */
export function registerAppScheme(): void {
  protocol.registerSchemesAsPrivileged([
    {
      scheme: SCHEME,
      privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true },
    },
  ])
}

/**
 * 在 app.whenReady 之后注册 app:// 请求处理器。
 *
 * @param root 渲染层产物根目录，必须是规范化的绝对或可解析路径。
 * @returns {void} 注册完成后不返回值。
 * @throws {Error} 请求路径无法解码、协议处理器注册失败或资源读取失败时由 Electron/URL API 抛出。
 * @sideEffects 注册协议处理器，并拒绝解析到 root 之外的路径穿越请求。
 */
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
