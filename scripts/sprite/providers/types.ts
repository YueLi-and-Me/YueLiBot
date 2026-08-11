/**
 * 定义图像生成供应商的统一输入、错误和调用接口。
 *
 * base、gen 和运行时生成脚本只依赖这些类型；供应商实现可替换协议而不改变素材生成
 * 管线的参数和错误分类。
 */

export interface GenerateOptions {
  /** 正向描述。管线里的中文指令由 config.ts 统一产出。 */
  prompt: string
  /** 参考图。用于角色一致性——传底图或用户的原始参考图。 */
  refs?: Buffer[]
  /** 部分厂商支持；不支持时静默忽略。 */
  seed?: number
}

export interface EditOptions {
  /** 待编辑的底图。 */
  base: Buffer
  /** 编辑指令，例如「保持一致，只把表情改成生气」。 */
  instruction: string
  seed?: number
}

export interface ImageProvider {
  readonly name: string
  /** 当前生效的模型 ID，用于日志与报错定位。 */
  readonly model: string
  /**
   * 根据文本提示和可选参考图生成图像。
   *
   * @param opts 生成提示和参考图参数；具体供应商可忽略不支持的可选字段。
   * @returns {Promise<Buffer>} 供应商返回并归一化后的图像二进制数据。
   * @throws {ProviderError|Error} 鉴权、网络、配额、协议解析或图像处理失败时抛出。
   */
  generate(opts: GenerateOptions): Promise<Buffer>
  /**
   * 根据底图和编辑指令生成保持主体结构的图像。
   *
   * @param opts 底图、编辑指令及可选种子参数。
   * @returns {Promise<Buffer>} 供应商返回并归一化后的图像二进制数据。
   * @throws {ProviderError|Error} 鉴权、网络、配额、协议解析或图像处理失败时抛出。
   */
  edit(opts: EditOptions): Promise<Buffer>
}

/**
 * 供应商失败原因分类，供重试策略、诊断输出和供应商切换逻辑统一使用。
 */
export type FailureKind =
  | 'auth' // Key 无效 / 未授权
  | 'region' // Key 有效，但来源地区被拒绝访问（大陆直连 Google 的典型症状）
  | 'quota' // 触发限流或配额耗尽
  | 'model' // 模型 ID 不存在或当前账号无权访问
  | 'network' // 连不上
  | 'blocked' // 被内容安全策略拦截
  | 'empty' // 请求成功但响应里没有图
  | 'unknown'

/**
 * 将鉴权类错误进一步区分为凭证无效和地区访问受限。
 * 前者需要更换凭证，后者需要配置代理或切换供应商；区分两类错误才能选择正确的处理路径。
 *
 * @param kind 初步失败类别。
 * @param detail 可选的供应商错误详情，大小写不敏感地检查地区限制关键词。
 * @returns {FailureKind} 当 ``kind`` 为 ``auth`` 且详情命中地区限制特征时返回 ``region``，
 *   否则返回原类别。
 */
export function refineKind(kind: FailureKind, detail?: string): FailureKind {
  if (kind !== 'auth' || !detail) return kind
  const d = detail.toLowerCase()
  const regionSigns = ['denied access', 'permission_denied', 'location is not supported', 'country, region, or territory', 'not available in your']
  return regionSigns.some((s) => d.includes(s)) ? 'region' : kind
}

export class ProviderError extends Error {
  /**
   * 创建带失败类别和可选原始详情的供应商错误。
   *
   * @param kind 可用于重试、切换或用户提示的失败类别。
   * @param message 面向日志和诊断的错误摘要。
   * @param detail 可选供应商响应片段。
   */
  constructor(
    readonly kind: FailureKind,
    message: string,
    readonly detail?: string,
  ) {
    super(message)
    this.name = 'ProviderError'
  }
}

/**
 * 判断供应商错误是否适合自动重试。
 *
 * @param err 待判断的未知错误对象。
 * @returns {boolean} 仅当错误是 ``ProviderError`` 且类别为 ``quota`` 或 ``network`` 时返回 ``true``。
 */
export function isRetryable(err: unknown): boolean {
  return err instanceof ProviderError && (err.kind === 'quota' || err.kind === 'network')
}
