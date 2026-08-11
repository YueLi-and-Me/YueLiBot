/**
 * 为 Node 网络请求配置全局 HTTP(S) 代理，并保证同一进程只安装一次 dispatcher。
 *
 * 图像供应商在发起请求前调用 setupProxy；本模块只读取代理环境变量，不修改业务
 * 配置和请求 payload。
 */
import { ProxyAgent, setGlobalDispatcher } from 'undici'

let applied: string | null = null

/**
 * 根据代理环境变量安装进程级 Undici dispatcher。
 *
 * @returns {string | null} 实际应用的代理地址；未配置代理时返回 ``null``，重复调用
 *   返回首次应用的结果。
 * @throws {Error} 代理地址格式非法或 Undici 无法创建代理 dispatcher 时抛出。
 * @remarks Node 内置 fetch 不会自动应用 ``HTTPS_PROXY`` 等环境变量，因此在任何
 *   Provider 发起网络请求前显式安装 dispatcher；函数只安装一次并缓存结果。
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
