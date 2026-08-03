import { ProxyAgent, setGlobalDispatcher } from 'undici'

let applied: string | null = null

/**
 * 大陆直连 generativelanguage.googleapis.com 基本不通，代理是常态。
 * Node 内置 fetch 默认不读 HTTPS_PROXY 环境变量，必须显式挂 dispatcher。
 * 在任何网络请求前调用一次即可。
 */
export function setupProxy(): string | null {
  if (applied !== null) return applied

  const url = process.env.HTTPS_PROXY || process.env.https_proxy || process.env.HTTP_PROXY || process.env.http_proxy
  if (!url?.trim()) {
    applied = ''
    return null
  }

  setGlobalDispatcher(new ProxyAgent(url.trim()))
  applied = url.trim()
  return applied
}
