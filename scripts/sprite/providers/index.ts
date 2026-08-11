/**
 * 图像供应商组装与公共导出模块。
 *
 * 本模块在加载时通过 `dotenv/config` 将项目环境变量载入进程，重新导出统一的
 * Provider 类型、代理配置和有限重试工具，并根据 `SPRITE_PROVIDER`、供应商密钥
 * 及模型环境变量创建 Gemini 或 Seedream Provider。具体请求与响应解析分别由
 * `gemini.ts`、`seedream.ts`、`http.ts` 和 `image.ts` 实现；上层立绘脚本只依赖
 * `ImageProvider` 接口和本模块的工厂函数。
 */
import 'dotenv/config'
import { GeminiProvider } from './gemini.ts'
import { setupProxy } from './proxy.ts'
import { SeedreamProvider } from './seedream.ts'
import { ProviderError, type ImageProvider } from './types.ts'

export * from './types.ts'
export { withRetry } from './http.ts'
export { setupProxy } from './proxy.ts'

/**
 * 根据显式选项或环境变量创建图像供应商实例。
 *
 * @param {{ provider?: string; model?: string }} opts 工厂配置，默认为空对象。
 *   `provider` 可选，取值为 `gemini`、`seedream` 或其别名 `ark`；未提供时读取
 *   `SPRITE_PROVIDER`，环境变量也未设置时默认为 `gemini`。`model` 可选，优先于
 *   对应供应商的模型环境变量传递；空白会由具体供应商处理。
 * @returns {ImageProvider} 已完成认证配置的图像供应商实例，其 `name` 和 `model`
 *   可用于日志、诊断和统一调用。
 * @throws {ProviderError} 供应商名称不在允许集合内，或所选供应商缺少必需 API 密钥
 *   时抛出。
 * @throws {Error} 代理环境变量存在但格式非法，导致全局代理安装失败时，传播代理
 *   配置错误。
 * @remarks 调用前会执行一次全局代理初始化；该步骤可能读取并安装进程级网络
 *   dispatcher，但不会发起图像生成请求。供应商实例化本身为常量级操作，首次网络
 *   请求在调用返回对象的 `generate` 或 `edit` 时才发生。
 */
export function createProvider(opts: { provider?: string; model?: string } = {}): ImageProvider {
  setupProxy()

  const which = (opts.provider || process.env.SPRITE_PROVIDER || 'gemini').trim().toLowerCase()

  switch (which) {
    case 'gemini':
      return new GeminiProvider({
        apiKey: process.env.GEMINI_API_KEY ?? '',
        model: opts.model || process.env.GEMINI_IMAGE_MODEL,
        baseUrl: process.env.GEMINI_BASE_URL,
      })
    case 'seedream':
    case 'ark':
      return new SeedreamProvider({
        apiKey: process.env.ARK_API_KEY ?? '',
        model: opts.model || process.env.ARK_IMAGE_MODEL,
        baseUrl: process.env.ARK_BASE_URL,
        size: process.env.ARK_IMAGE_SIZE,
      })
    default:
      throw new ProviderError('unknown', `未知的 SPRITE_PROVIDER：${which}`, '可选值：gemini | seedream')
  }
}
