import { classifyStatus, postJson } from './http.ts'
import { toPng } from './image.ts'
import { ProviderError, type EditOptions, type FailureKind, type GenerateOptions, type ImageProvider } from './types.ts'

/**
 * 火山引擎方舟（Seedream / 豆包）provider —— 国内直连备选。
 *
 * 接口是 OpenAI images 兼容形态：POST /images/generations，
 * 参考图通过 `image` 传 data URI（支持数组做多图参考），
 * 响应取 data[0].b64_json。
 */
/**
 * 方舟的错误码比 HTTP 状态码有信息量得多。
 * 尤其是内容拦截会返回 400 —— 按状态码只能归成 unknown，
 * 用户看到「HTTP 400」完全不知道该改什么。
 */
function classifyArkError(status: number, text: string): { kind: FailureKind; message: string } {
  let code = ''
  try {
    code = (JSON.parse(text) as { error?: { code?: string } }).error?.code ?? ''
  } catch {
    /* 非 JSON 响应，退回按状态码分类 */
  }

  if (/SensitiveContent|Sensitive|Risk|Policy/i.test(code)) {
    const where = /Input/i.test(code) ? '输入图片' : /Output/i.test(code) ? '生成结果' : '请求内容'
    return { kind: 'blocked', message: `方舟内容审核拦截：${where}被判定为敏感内容（${code}）` }
  }
  if (/Quota|RateLimit|TPM|RPM|Throttl/i.test(code)) return { kind: 'quota', message: `方舟限流或配额不足（${code}）` }
  if (/Auth|ApiKey|Credential|Permission/i.test(code)) return { kind: 'auth', message: `方舟鉴权失败（${code}）` }
  if (/Model|Endpoint/i.test(code)) return { kind: 'model', message: `方舟模型不可用（${code}）` }

  return { kind: classifyStatus(status), message: `方舟返回 HTTP ${status}${code ? `（${code}）` : ''}` }
}

export class SeedreamProvider implements ImageProvider {
  readonly name = 'seedream'
  readonly model: string
  private readonly apiKey: string
  private readonly baseUrl: string
  private readonly size: string

  constructor(opts: { apiKey: string; model?: string; baseUrl?: string; size?: string }) {
    if (!opts.apiKey?.trim()) {
      throw new ProviderError('auth', 'ARK_API_KEY 未设置', '请在 .env 中填入火山引擎方舟的 API Key')
    }
    this.apiKey = opts.apiKey.trim()
    // 5.0 Pro 做指令编辑几乎零漂移（实测高度偏移 0px，4.0 是 +21px），
    // 代价是慢约 10 倍。差分质量的天花板由它决定，值这个等待
    this.model = opts.model?.trim() || 'doubao-seedream-5-0-pro-260628'
    this.baseUrl = (opts.baseUrl?.trim() || 'https://ark.cn-beijing.volces.com/api/v3').replace(/\/+$/, '')
    // 全身立绘要竖构图。默认给 2:3，方形会逼模型把角色压扁或裁掉腿
    this.size = opts.size?.trim() || '1440x2160'
  }

  async generate({ prompt, refs = [] }: GenerateOptions): Promise<Buffer> {
    return this.call(prompt, refs)
  }

  async edit({ base, instruction }: EditOptions): Promise<Buffer> {
    return this.call(instruction, [base])
  }

  private async call(prompt: string, refs: Buffer[]): Promise<Buffer> {
    const images = refs.map((b) => `data:image/png;base64,${b.toString('base64')}`)

    const body: Record<string, unknown> = {
      model: this.model,
      prompt,
      response_format: 'b64_json',
      size: this.size,
      watermark: false,
    }
    // 单图传字符串、多图传数组 —— 两种都被接受，但单图用字符串兼容性更好
    if (images.length === 1) body.image = images[0]
    else if (images.length > 1) body.image = images

    const { status, text } = await postJson(`${this.baseUrl}/images/generations`, { Authorization: `Bearer ${this.apiKey}` }, body)

    if (status !== 200) {
      const { kind, message } = classifyArkError(status, text)
      throw new ProviderError(kind, message, text.slice(0, 400))
    }

    let json: { data?: Array<{ b64_json?: string; url?: string }>; error?: { message?: string } }
    try {
      json = JSON.parse(text)
    } catch {
      throw new ProviderError('unknown', '方舟响应不是合法 JSON', text.slice(0, 400))
    }

    if (json.error?.message) {
      throw new ProviderError('unknown', `方舟报错：${json.error.message}`)
    }

    const first = json.data?.[0]
    if (first?.b64_json) return toPng(Buffer.from(first.b64_json, 'base64'))

    // 部分配置下只返回 URL，补一次下载
    if (first?.url) {
      const res = await fetch(first.url)
      if (!res.ok) throw new ProviderError('network', `下载生成结果失败：HTTP ${res.status}`)
      return toPng(Buffer.from(await res.arrayBuffer()))
    }

    throw new ProviderError('empty', '响应中没有图像数据', text.slice(0, 400))
  }
}
