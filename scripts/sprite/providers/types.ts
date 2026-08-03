/** 生图 provider 的统一接口。上层（gen/base/test）只认这个，换厂商不影响调用方。 */

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
  generate(opts: GenerateOptions): Promise<Buffer>
  /** 指令式编辑 —— 管线主力。表情差分全靠它保持一致性。 */
  edit(opts: EditOptions): Promise<Buffer>
}

/**
 * 失败原因分类。冒烟测试靠它给出人话诊断，而不是甩一个 HTTP 状态码。
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
 * 把 403 细分成「Key 不对」和「地区被拒」——两者的处理方式完全不同：
 * 前者重新领 Key，后者挂代理或换 provider。混为一谈会让人白折腾很久。
 */
export function refineKind(kind: FailureKind, detail?: string): FailureKind {
  if (kind !== 'auth' || !detail) return kind
  const d = detail.toLowerCase()
  const regionSigns = ['denied access', 'permission_denied', 'location is not supported', 'country, region, or territory', 'not available in your']
  return regionSigns.some((s) => d.includes(s)) ? 'region' : kind
}

export class ProviderError extends Error {
  constructor(
    readonly kind: FailureKind,
    message: string,
    readonly detail?: string,
  ) {
    super(message)
    this.name = 'ProviderError'
  }
}

/** 网络层抖动和限流值得重试；鉴权、模型不存在、内容拦截重试多少次都一样。 */
export function isRetryable(err: unknown): boolean {
  return err instanceof ProviderError && (err.kind === 'quota' || err.kind === 'network')
}
