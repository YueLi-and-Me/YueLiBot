/**
 * Gemini 图像供应商适配模块。
 *
 * 本模块将文本提示和 PNG 参考图转换为 Gemini 的多模态请求，兼容
 * `generateContent` 与 `interactions` 两种接口形态，并从响应中提取图像数据后
 * 统一编码为 PNG。`GeminiProvider` 实现 `ImageProvider`，供立绘生成、编辑和
 * 差分处理脚本调用。
 *
 * 请求发送与 HTTP 状态分类依赖同目录的 `http.ts`，错误类型依赖 `types.ts`，
 * 图像格式归一化依赖 `image.ts`。本模块只负责接口形态探测和响应解析，不在
 * `call` 中自动执行指数退避重试；重试由上层按需使用 `http.ts` 提供的工具。
 */
import { classifyStatus, postJson } from './http.ts'
import { toPng } from './image.ts'
import { ProviderError, type EditOptions, type GenerateOptions, type ImageProvider } from './types.ts'

/**
 * Gemini 图像供应商实现。
 *
 * 该类实现统一的 `ImageProvider` 接口，负责构造多模态请求、探测可用的接口形态、
 * 分类供应商错误并提取响应中的图像数据。首次请求成功的接口形态会缓存在实例中，
 * 后续请求直接复用；响应解析支持已知字段和嵌套对象遍历两条路径。
 */

type Shape = 'generateContent' | 'interactions'

/** 探测顺序。generateContent 是模型实际声明支持的方法，优先。 */
const SHAPES: readonly Shape[] = ['generateContent', 'interactions']

interface Part {
  text?: string
  image?: Buffer
}

/**
 * Gemini 图像生成与编辑的统一 Provider 实现。
 *
 * 实例负责保存认证和接口配置，并在首次请求时确定可用的协议形态；后续调用通过
 * `ImageProvider` 暴露的 `generate` 和 `edit` 方法返回 PNG 图像数据。
 */
export class GeminiProvider implements ImageProvider {
  readonly name = 'gemini'
  readonly model: string
  private readonly apiKey: string
  private readonly baseUrl: string
  private shape: Shape | null = null

  /**
   * 创建 Gemini 图像供应商实例并保存请求配置。
   *
   * @param {{ apiKey: string; model?: string; baseUrl?: string }} opts 配置对象。
   *   `apiKey` 为必填的 Gemini API 密钥；`model` 为可选模型标识，默认使用
   *   `gemini-3.1-flash-image`；`baseUrl` 为可选接口根地址，默认使用官方
   *   Generative Language API 地址，末尾斜杠会被移除。
   * @throws {ProviderError} `apiKey` 去除首尾空白后为空时，抛出 `auth` 类错误。
   * @remarks 仅规范化并保存认证、模型和地址配置，不发起网络请求。字符串处理和
   *   状态初始化为常量级开销。
   */
  constructor(opts: { apiKey: string; model?: string; baseUrl?: string }) {
    if (!opts.apiKey?.trim()) {
      throw new ProviderError('auth', 'GEMINI_API_KEY 未设置', '请在 .env 中填入，从 https://aistudio.google.com/apikey 领取')
    }
    this.apiKey = opts.apiKey.trim()
    this.model = opts.model?.trim() || 'gemini-3.1-flash-image'
    this.baseUrl = (opts.baseUrl?.trim() || 'https://generativelanguage.googleapis.com').replace(/\/+$/, '')
  }

  /**
   * 根据文本提示和可选参考图请求一张图像。
   *
   * @param {string} prompt 生成指令；可以为空字符串，内容校验由供应商接口负责，
   *   本方法不额外限制长度或字符集。
   * @param {Buffer[]} refs 参考图二进制列表，默认为空列表；每个元素应为完整图像
   *   Buffer，调用时会按 PNG 数据写入请求体，本方法不限制数量和文件大小。
   * @returns {Promise<Buffer>} 供应商响应转换后的 PNG 图像数据。
   * @throws {ProviderError} 网络、鉴权、配额、模型、内容安全、协议解析或空图像
   *   响应导致请求失败时抛出。
   * @throws {Error} 已缓存接口形态下，底层图像解码或编码失败时可能传播图像处理库
   *   抛出的错误。
   * @remarks 首次调用可能探测两种接口形态，后续调用复用已成功的形态；请求体会
   *   为每张参考图创建 base64 副本，内存开销与参考图总字节数成正比。输入对象中
   *   的 `seed` 字段不参与本实现的请求构造。
   */
  async generate({ prompt, refs = [] }: GenerateOptions): Promise<Buffer> {
    return this.call([{ text: prompt }, ...refs.map((image) => ({ image }))])
  }

  /**
   * 根据底图和编辑指令请求一张保持主体结构的编辑结果。
   *
   * @param {Buffer} base 待编辑底图的完整图像数据；本方法不预先验证格式，数据
   *   会以 PNG 内联图像写入请求体。
   * @param {string} instruction 编辑指令；本方法不限制长度或字符集。
   * @returns {Promise<Buffer>} 供应商响应转换后的 PNG 图像数据。
   * @throws {ProviderError} 网络、鉴权、配额、模型、内容安全、协议解析或空图像
   *   响应导致请求失败时抛出。
   * @throws {Error} 已缓存接口形态下，底层图像解码或编码失败时可能传播图像处理库
   *   抛出的错误。
   * @remarks 将文本指令放在图像数据之前，以保持多模态请求中指令与编辑目标的
   *   对应关系；请求会产生网络和 base64 编码开销。输入对象中的 `seed` 字段不参与
   *   本实现的请求构造。
   */
  async edit({ base, instruction }: EditOptions): Promise<Buffer> {
    // 先发送编辑指令再发送底图，以保持供应商对“编辑目标”和“约束条件”的输入顺序。
    return this.call([{ text: instruction }, { image: base }])
  }

  /**
   * 选择已探测的接口形态或按固定顺序探测所有形态。
   *
   * @param {Part[]} parts 文本和参考图组成的多模态输入；数组顺序会原样传递给
   *   请求构造函数，至少应包含一个文本或图像部分。
   * @returns {Promise<Buffer>} 成功响应转换后的 PNG 图像数据。
   * @throws {ProviderError} 所有接口形态均失败时抛出；若任一请求被分类为配额错误，
   *   会立即终止探测并抛出，不会切换到另一接口形态。
   * @remarks `generateContent` 按固定顺序优先探测，原因是模型能力声明通常以该
   *   方法名表达；错误端点也可能返回 403，不能仅凭 403 直接判定凭证无效，否则
   *   会跳过仍可能可用的另一形态。首次成功后缓存形态，后续调用从两次网络请求
   *   降为一次；探测期间最多发送两次请求。
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

    // 两种形态均失败时优先保留已分类错误，确保调用方能够定位失败原因。
    throw errors.find((e) => e.kind !== 'unknown') ?? errors[0]!
  }

  /**
   * 按指定 Gemini 接口形态发送一次请求，并将响应解析为 PNG 图像。
   *
   * @param {Shape} shape 请求协议形态；只能取 `generateContent` 或 `interactions`。
   * @param {Part[]} parts 文本和图像输入；图像会转换为 base64，文本缺省值会按空
   *   字符串编码。
   * @returns {Promise<Buffer>} 响应中的图像经 PNG 归一化后的二进制数据。
   * @throws {ProviderError} HTTP 状态非 200、响应 JSON 无法解析、触发内容安全策略、
   *   响应不含图像数据或网络请求失败时抛出。
   * @throws {Error} 图像数据无法被底层图像库解码或编码时可能传播底层错误。
   * @remarks 解析会先检查已知安全拦截字段，再提取图像并调用 `toPng`；请求和
   *   base64 转换会产生网络等待及与输入图像大小成正比的内存开销。
   */
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

  /**
   * 将多模态输入转换为 `interactions` 接口请求描述。
   *
   * @param {Part[]} parts 按发送顺序排列的文本和图像部分；图像按 PNG MIME 类型
   *   转换为 base64 字符串。
   * @returns {{ url: string; body: Record<string, unknown> }} 包含请求 URL 和 JSON
   *   请求体的描述对象，不执行网络请求。
   * @remarks 会复制图像数据的 base64 表示，时间和临时内存开销与图像总字节数成正比。
   */
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

  /**
   * 将多模态输入转换为 `generateContent` 接口请求描述。
   *
   * @param {Part[]} parts 按发送顺序排列的文本和图像部分；图像写入
   *   `inline_data.data`，文本写入 `text`。
   * @returns {{ url: string; body: Record<string, unknown> }} 包含模型 URL 和 JSON
   *   请求体的描述对象，不执行网络请求。
   * @remarks 请求体声明同时需要文本和图像响应模态；图像 base64 编码会产生与输入
   *   大小成正比的临时字符串。
   */
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

/**
 * 判断未知响应字段是否满足图像 base64 数据的最小特征。
 *
 * @param {unknown} v 待检查的未知响应字段。
 * @returns {boolean} 当字段为长度大于 1024 的字符串，且前 256 个字符仅包含
 *   base64 字符或空白时返回 `true`；否则返回 `false`。
 * @remarks 只检查前 256 个字符，以较低成本排除普通短文本字段；该检查不是完整的
 *   base64 解码验证，最终有效性由图像解码步骤确认。
 */
function looksLikeImageData(v: unknown): v is string {
  return typeof v === 'string' && v.length > 1024 && /^[A-Za-z0-9+/=\s]+$/.test(v.slice(0, 256))
}

/**
 * 从未知结构的 Gemini 响应中提取图像 base64 数据并解码。
 *
 * @param {unknown} json 已解析的 JSON 响应，可以是对象、数组或嵌套结构。
 * @returns {Buffer | null} 找到候选 base64 字符串并转换后的二进制数据；未找到候选
 *   字段时返回 `null`。图像格式有效性由后续 `toPng` 调用验证。
 * @remarks 先检查已知响应字段，避免正常响应遍历完整对象树；未命中时再递归检查
 *   其他对象值，以兼容新增包裹层。`seen` 集合用于避免异常循环引用造成无限递归，
 *   但完整遍历仍可能随响应节点数线性增长，并会复制 base64 解码结果。
 */
function extractImage(json: unknown): Buffer | null {
  const seen = new Set<unknown>()

  /**
   * 深度优先遍历响应节点并返回第一个可识别的图像 base64 字符串。
   *
   * @param {unknown} node 当前待检查的响应节点。
   * @returns {string | null} 图像 base64 字符串；当前分支未找到时返回 `null`。
   * @remarks 使用引用集合跳过已访问对象，避免同一对象在非标准运行时响应结构中被
   *   重复扫描。
   */
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

    // 优先读取已知响应字段，减少对完整响应树的递归遍历。
    for (const key of ['output_image', 'outputImage', 'inlineData', 'inline_data']) {
      const child = obj[key]
      if (child && typeof child === 'object') {
        const data = (child as Record<string, unknown>).data
        if (looksLikeImageData(data)) return data
      }
    }

    // 对带有图像 MIME 类型的通用 data 节点进行兼容解析。
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

/**
 * 从 Gemini 响应中提取内容安全拦截原因。
 *
 * @param {unknown} json 已解析的 JSON 响应。
 * @returns {string | null} 安全策略返回的拦截原因；响应未包含已知拦截字段时返回
 *   `null`。候选列表仅检查第一个候选项，因为当前请求只要求单张图像结果。
 * @remarks 同时检查驼峰和下划线命名，以兼容两种接口形态；只读取字段，不修改
 *   响应对象。
 */
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

/**
 * 将错误详情限制为指定最大长度，避免异常信息污染终端输出。
 *
 * @param {string} s 原始错误文本。
 * @param {number} n 最大字符数，默认为 `400`；调用方应传入非负整数，本函数不
 *   对该参数进行范围校验。
 * @returns {string} 不超过限制的文本；超出限制时在截断内容后追加省略号。
 * @remarks 仅创建不超过限制长度的字符串，避免将完整供应商响应写入异常和终端日志。
 */
function truncate(s: string, n = 400): string {
  return s.length > n ? `${s.slice(0, n)}…` : s
}
