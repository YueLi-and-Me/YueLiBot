import { classifyStatus, postJson } from './http.ts'
import { toPng } from './image.ts'
import { ProviderError, type EditOptions, type GenerateOptions, type ImageProvider } from './types.ts'

/**
 * Gemini 图像模型 provider。
 *
 * Google 的图像接口存在两种请求形态，且随版本变动：
 *   A) POST /v1beta/interactions              —— 较新的统一 interactions 接口
 *   B) POST /v1beta/models/{model}:generateContent —— 经典 generateContent 接口
 *
 * 这里两种都实现：首次调用探测 A，失败（404 / 400 形态不符）则退到 B，
 * 并把成功的那个记住，后续请求不再重复探测。
 * 响应解析同样做了兼容——先按已知字段找，找不到就递归搜 base64 图像块。
 */

type Shape = 'generateContent' | 'interactions'

/** 探测顺序。generateContent 是模型实际声明支持的方法，优先。 */
const SHAPES: readonly Shape[] = ['generateContent', 'interactions']

interface Part {
  text?: string
  image?: Buffer
}

export class GeminiProvider implements ImageProvider {
  readonly name = 'gemini'
  readonly model: string
  private readonly apiKey: string
  private readonly baseUrl: string
  private shape: Shape | null = null

  constructor(opts: { apiKey: string; model?: string; baseUrl?: string }) {
    if (!opts.apiKey?.trim()) {
      throw new ProviderError('auth', 'GEMINI_API_KEY 未设置', '请在 .env 中填入，从 https://aistudio.google.com/apikey 领取')
    }
    this.apiKey = opts.apiKey.trim()
    this.model = opts.model?.trim() || 'gemini-3.1-flash-image'
    this.baseUrl = (opts.baseUrl?.trim() || 'https://generativelanguage.googleapis.com').replace(/\/+$/, '')
  }

  async generate({ prompt, refs = [] }: GenerateOptions): Promise<Buffer> {
    return this.call([{ text: prompt }, ...refs.map((image) => ({ image }))])
  }

  async edit({ base, instruction }: EditOptions): Promise<Buffer> {
    // 指令在前、图在后：实测这个顺序对「保持一致」类指令的遵循度更好
    return this.call([{ text: instruction }, { image: base }])
  }

  /**
   * 按当前已知形态发请求；形态未知时逐个探测。
   *
   * generateContent 排在前面：模型自己在 ListModels 里声明的
   * supportedGenerationMethods 就是它，而 interactions 并不在其中。
   *
   * 探测阶段对失败要足够宽容 —— 走错端点时 Google 返回的是 403，
   * 若按「403 = 鉴权失败」直接放弃，就永远轮不到第二种形态。
   * 唯一不值得换形态重试的是 quota：换个端点一样会被限流。
   */
  private async call(parts: Part[]): Promise<Buffer> {
    if (this.shape) return this.request(this.shape, parts)

    const errors: ProviderError[] = []
    for (const shape of SHAPES) {
      try {
        const out = await this.request(shape, parts)
        this.shape = shape
        return out
      } catch (err) {
        if (err instanceof ProviderError && err.kind === 'quota') throw err
        errors.push(err instanceof ProviderError ? err : new ProviderError('unknown', String(err)))
      }
    }

    // 两种形态都失败：优先抛分类明确的那个，报错才有指向性
    throw errors.find((e) => e.kind !== 'unknown') ?? errors[0]!
  }

  private async request(shape: Shape, parts: Part[]): Promise<Buffer> {
    const { url, body } = shape === 'interactions' ? this.buildInteractions(parts) : this.buildGenerateContent(parts)

    const { status, text } = await postJson(url, { 'x-goog-api-key': this.apiKey }, body)

    if (status !== 200) {
      throw new ProviderError(classifyStatus(status), `Gemini 返回 HTTP ${status}`, truncate(text))
    }

    let json: unknown
    try {
      json = JSON.parse(text)
    } catch {
      throw new ProviderError('unknown', 'Gemini 响应不是合法 JSON', truncate(text))
    }

    const blocked = findBlockReason(json)
    if (blocked) {
      throw new ProviderError('blocked', `内容被安全策略拦截：${blocked}`, '试着弱化指令中的敏感描述，或换一张参考图')
    }

    const image = extractImage(json)
    if (!image) {
      throw new ProviderError('empty', '响应中没有图像数据', truncate(text))
    }
    return toPng(image)
  }

  private buildInteractions(parts: Part[]) {
    return {
      url: `${this.baseUrl}/v1beta/interactions`,
      body: {
        model: this.model,
        input: parts.map((p) =>
          p.image
            ? { type: 'image', mime_type: 'image/png', data: p.image.toString('base64') }
            : { type: 'text', text: p.text ?? '' },
        ),
      },
    }
  }

  private buildGenerateContent(parts: Part[]) {
    return {
      url: `${this.baseUrl}/v1beta/models/${this.model}:generateContent`,
      body: {
        contents: [
          {
            role: 'user',
            parts: parts.map((p) =>
              p.image
                ? { inline_data: { mime_type: 'image/png', data: p.image.toString('base64') } }
                : { text: p.text ?? '' },
            ),
          },
        ],
        generationConfig: { responseModalities: ['TEXT', 'IMAGE'] },
      },
    }
  }
}

/** base64 图像块：长度足够且只含 base64 字符。阈值取 1KB，滤掉短字符串字段。 */
function looksLikeImageData(v: unknown): v is string {
  return typeof v === 'string' && v.length > 1024 && /^[A-Za-z0-9+/=\s]+$/.test(v.slice(0, 256))
}

/**
 * 从响应里挖出图像。先按两种已知形态取，取不到就递归找
 * ——接口字段名换了也不至于整条管线报废。
 */
function extractImage(json: unknown): Buffer | null {
  const seen = new Set<unknown>()

  const walk = (node: unknown): string | null => {
    if (!node || typeof node !== 'object' || seen.has(node)) return null
    seen.add(node)

    if (Array.isArray(node)) {
      for (const item of node) {
        const hit = walk(item)
        if (hit) return hit
      }
      return null
    }

    const obj = node as Record<string, unknown>

    // 形态 A：output_image.data
    // 形态 B：parts[].inlineData.data / inline_data.data
    for (const key of ['output_image', 'outputImage', 'inlineData', 'inline_data']) {
      const child = obj[key]
      if (child && typeof child === 'object') {
        const data = (child as Record<string, unknown>).data
        if (looksLikeImageData(data)) return data
      }
    }

    // 兜底：本节点自带 data 且 mime 是 image/*
    const mime = obj.mime_type ?? obj.mimeType
    if (typeof mime === 'string' && mime.startsWith('image/') && looksLikeImageData(obj.data)) {
      return obj.data
    }

    for (const value of Object.values(obj)) {
      const hit = walk(value)
      if (hit) return hit
    }
    return null
  }

  const b64 = walk(json)
  return b64 ? Buffer.from(b64.replace(/\s/g, ''), 'base64') : null
}

/** 安全拦截在两种形态下字段名不同，都查一遍。 */
function findBlockReason(json: unknown): string | null {
  if (!json || typeof json !== 'object') return null
  const root = json as Record<string, unknown>

  const feedback = root.promptFeedback ?? root.prompt_feedback
  if (feedback && typeof feedback === 'object') {
    const reason = (feedback as Record<string, unknown>).blockReason ?? (feedback as Record<string, unknown>).block_reason
    if (typeof reason === 'string') return reason
  }

  const candidates = root.candidates
  if (Array.isArray(candidates) && candidates[0] && typeof candidates[0] === 'object') {
    const finish = (candidates[0] as Record<string, unknown>).finishReason
    if (typeof finish === 'string' && ['SAFETY', 'PROHIBITED_CONTENT', 'BLOCKLIST'].includes(finish)) return finish
  }
  return null
}

function truncate(s: string, n = 400): string {
  return s.length > n ? `${s.slice(0, n)}…` : s
}
