import 'dotenv/config'
import { GeminiProvider } from './gemini.ts'
import { setupProxy } from './proxy.ts'
import { SeedreamProvider } from './seedream.ts'
import { ProviderError, type ImageProvider } from './types.ts'

export * from './types.ts'
export { withRetry } from './http.ts'
export { setupProxy } from './proxy.ts'

/**
 * 按 .env 里的 SPRITE_PROVIDER 造 provider。切厂商只改一行配置，
 * 上层 base/gen/test 全部无感。
 *
 * `model` 用于按场景换档：跑底图候选时本来就要 4 张各不相同的图，
 * 一致性毫无意义，用快模型；只有做差分才需要慢而稳的那个。
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
