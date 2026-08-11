/**
 * Seedream 图像供应商适配模块。
 *
 * 本模块将统一的生成和编辑参数转换为方舟 `POST /images/generations` 请求体，支持
 * 单图字符串和多图数组形式的参考图，并兼容 `data[0].b64_json` 与 `data[0].url`
 * 两种结果返回方式。响应图像统一转换为 PNG 后交给立绘生成管线，调用方无需依赖
 * 供应商的认证字段和响应结构。
 *
 * HTTP 请求和状态分类依赖同目录的 `http.ts`，统一错误类型依赖 `types.ts`，图像
 * 格式归一化依赖 `image.ts`。本模块不自动执行重试；`http.ts` 导出的重试工具由
 * 上层按需调用。
 */
import { classifyStatus, postJson } from './http.ts'
import { toPng } from './image.ts'
import { ProviderError, type EditOptions, type FailureKind, type GenerateOptions, type ImageProvider } from './types.ts'

/**
 * 将方舟响应中的供应商错误码映射为统一失败类别。
 *
 * @param {number} status HTTP 响应状态码；用于错误码缺失或无法识别时的通用分类。
 * @param {string} text HTTP 响应正文；优先解析其中 `error.code`，正文不是合法 JSON
 *   时按状态码分类。
 * @returns {{ kind: FailureKind; message: string }} 统一失败类别及面向日志和诊断的
 *   中文错误摘要。
 * @remarks 先依据供应商错误码识别内容审核、配额、鉴权和模型错误，因为这些信息
 *   比单独的 HTTP 状态码更能决定后续处理路径；JSON 解析失败不会再次抛出，而是
 *   保留状态码分类结果。处理时间与响应正文长度成正比，且会短暂解析响应 JSON。
 */
function classifyArkError(status: number, text: string): { kind: FailureKind; message: string } {
  let code = ''
  try {
    code = (JSON.parse(text) as { error?: { code?: string } }).error?.code ?? ''
  } catch {
    /* 响应正文不是 JSON 时保留 HTTP 状态分类，避免错误处理因解析失败中断。 */
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

/**
 * Seedream 图像生成与编辑的统一 Provider 实现。
 *
 * 实例负责保存认证、模型、服务地址和输出尺寸，并将供应商响应转换为统一的 PNG
 * Buffer；网络请求仅在调用 `generate` 或 `edit` 时发起。
 */
export class SeedreamProvider implements ImageProvider {
  readonly name = 'seedream'
  readonly model: string
  private readonly apiKey: string
  private readonly baseUrl: string
  private readonly size: string

  /**
   * 创建 Seedream 图像供应商实例并保存请求参数。
   *
   * @param {{ apiKey: string; model?: string; baseUrl?: string; size?: string }} opts 配置
   *   对象。`apiKey` 为必填 API 密钥；`model` 可选，默认使用
   *   `doubao-seedream-5-0-pro-260628`；`baseUrl` 可选，默认使用方舟 API 根地址；
   *   `size` 可选，默认使用 `1440x2160`，本构造函数不校验尺寸字符串的格式或取值范围。
   * @throws {ProviderError} `apiKey` 去除首尾空白后为空时，抛出 `auth` 类错误。
   * @remarks 只规范化并保存认证、模型、地址和尺寸配置，不发起网络请求。末尾斜杠
   *   清理和字符串复制为常量级开销。
   */
  constructor(opts: { apiKey: string; model?: string; baseUrl?: string; size?: string }) {
    if (!opts.apiKey?.trim()) {
      throw new ProviderError('auth', 'ARK_API_KEY 未设置', '请在 .env 中填入火山引擎方舟的 API Key')
    }
    this.apiKey = opts.apiKey.trim()
    // 默认模型用于保证编辑结果的主体位置稳定；模型可由配置覆盖以适应供应商变更。
    this.model = opts.model?.trim() || 'doubao-seedream-5-0-pro-260628'
    this.baseUrl = (opts.baseUrl?.trim() || 'https://ark.cn-beijing.volces.com/api/v3').replace(/\/+$/, '')
    // 全身立绘使用 2:3 竖幅，降低方形画布导致主体压缩或裁切的概率。
    this.size = opts.size?.trim() || '1440x2160'
  }

  /**
   * 根据文本提示和可选参考图请求一张图像。
   *
   * @param {string} prompt 生成指令；本方法不限制长度或字符集，具体校验由供应商
   *   接口负责。
   * @param {Buffer[]} refs 参考图列表，默认为空列表；每个元素会按 PNG 数据 URI
   *   写入请求体，本方法不限制数量和文件大小。
   * @returns {Promise<Buffer>} 供应商响应转换后的 PNG 图像数据。
   * @throws {ProviderError} HTTP、鉴权、配额、模型、内容审核、协议解析或空图像
   *   响应导致请求失败时抛出。
   * @throws {Error} 下载 URL 图像或底层图像解码、编码失败时可能传播底层错误。
   * @remarks 每次调用都会进行网络请求，并按参考图数量生成 data URI；base64 编码会
   *   产生与参考图总大小成正比的临时内存开销。输入对象中的 `seed` 字段不参与本
   *   实现的请求构造。
   */
  async generate({ prompt, refs = [] }: GenerateOptions): Promise<Buffer> {
    return this.call(prompt, refs)
  }

  /**
   * 根据底图和编辑指令请求一张图像编辑结果。
   *
   * @param {Buffer} base 待编辑底图的完整图像数据；调用时作为唯一参考图发送，本方法不预先验证格式。
   * @param {string} instruction 编辑指令；本方法不限制长度或字符集。
   * @returns {Promise<Buffer>} 供应商响应转换后的 PNG 图像数据。
   * @throws {ProviderError} HTTP、鉴权、配额、模型、内容审核、协议解析或空图像响应导致请求失败时抛出。
   * @throws {Error} 下载 URL 图像或底层图像解码、编码失败时可能传播底层错误。
   * @remarks 编辑请求仍通过同一生成端点发送，输入底图会先编码为 PNG data URI，
   *   因此请求体和内存开销与底图大小成正比。输入对象中的 `seed` 字段不参与本实现
   *   的请求构造。
   */
  async edit({ base, instruction }: EditOptions): Promise<Buffer> {
    return this.call(instruction, [base])
  }

  /**
   * 构造方舟图像请求、解析响应并转换为 PNG。
   *
   * @param {string} prompt 生成或编辑指令；调用方负责保证其业务语义和长度满足供应商要求，本方法不执行内容校验。
   * @param {Buffer[]} refs 参考图列表；空列表不设置 `image` 字段，单图使用字符串，多图使用字符串数组，元素按 PNG data URI 编码。
   * @returns {Promise<Buffer>} 响应中的 base64 或 URL 图像经 PNG 归一化后的数据。
   * @throws {ProviderError} HTTP 状态、供应商错误码、JSON 解析、响应错误字段或空图像响应导致请求失败时抛出。
   * @throws {Error} URL 图像下载失败，或图像数据不能被底层图像库解码、编码时可能传播底层错误；URL 分支的下载不经过 `postJson` 的超时封装。
   * @remarks 每次调用至少发送一次 POST；响应只提供 URL 时还会额外发起一次下载。base64 输入和输出转换会产生与图像大小成正比的网络传输和内存开销。
   */
  private async call(prompt: string, refs: Buffer[]): Promise<Buffer> {
    const images = refs.map((b) => `data:image/png;base64,${b.toString('base64')}`)

    const body: Record<string, unknown> = {
      model: this.model,
      prompt,
      response_format: 'b64_json',
      size: this.size,
      watermark: false,
    }
    // 单图使用字符串、多图使用数组，按供应商接口的字段类型约定选择请求形状。
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

    // 响应仅提供 URL 时再发起一次下载，保持 Provider 接口仍返回图像二进制数据。
    if (first?.url) {
      const res = await fetch(first.url)
      if (!res.ok) throw new ProviderError('network', `下载生成结果失败：HTTP ${res.status}`)
      return toPng(Buffer.from(await res.arrayBuffer()))
    }

    throw new ProviderError('empty', '响应中没有图像数据', text.slice(0, 400))
  }
}
