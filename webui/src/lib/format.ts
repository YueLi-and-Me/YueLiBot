/**
 * 观察面板的数据收窄与展示格式化工具。
 *
 * 后端快照字段为弱类型 JSON，本模块提供 record/numeric/text 等收窄函数，以及
 * 中文时长、时间戳、发送者标签等展示文本的格式化；被 features 下全部业务
 * 组件依赖。逻辑移植自旧版渲染入口，行为保持一致。
 */
import type { ObservabilityStream, TraceEntry } from '../../../electron/shared/ipc.ts'

/**
 * 将未知快照字段收窄为非数组对象。
 *
 * @param value 待转换的未知值。
 * @returns 输入为非空对象时返回其记录视图，否则返回空记录。
 */
export function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

/**
 * 读取有限数字快照字段。
 *
 * @param value 待转换的未知值。
 * @returns 有限数字本身；类型不符或数值非有限时返回 `null`。
 */
export function numeric(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/**
 * 将快照值转换为适合页面展示的文本。
 *
 * @param value 待展示的未知值。
 * @returns 非空字符串、布尔值或有限数字的文本表示；其他值返回占位符 `—`。
 */
export function text(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return '—'
}

/**
 * 读取可选文本字段并去除首尾空白。
 *
 * @param value 待转换的未知值。
 * @returns 字符串的去空白结果；非字符串返回空字符串。
 */
export function optionalText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/**
 * 组合联系人显示名、平台昵称、外部账号和群名片。
 *
 * @param displayName 联系人显示名。
 * @param nickname 平台昵称。
 * @param externalId 外部平台账号标识；为空时不追加账号信息。
 * @param groupCard 群名片；非空且不同于昵称时优先展示群名片。
 * @returns 适合页面展示的发送者标签。
 */
export function qqSenderLabel(
  displayName: string,
  nickname: string,
  externalId: string,
  groupCard: string,
): string {
  if (!externalId) return displayName
  if (groupCard && groupCard !== nickname) {
    return `${groupCard}（QQ昵称：${nickname} · QQ号：${externalId}）`
  }
  return `${nickname || displayName}（QQ号：${externalId}）`
}

/**
 * 从追踪事件解析发送者标签，并为缺失字段提供平台级回退文本。
 *
 * @param entry 单条追踪事件。
 * @returns 发送者可读名称。
 */
export function traceSenderLabel(entry: TraceEntry): string {
  const ready = optionalText(entry.senderLabel)
  if (ready) return ready
  if (entry.platform === 'desktop') return '你'
  return qqSenderLabel(
    optionalText(entry.senderDisplayName) || `联系人 #${text(entry.personId)}`,
    optionalText(entry.senderNickname),
    optionalText(entry.senderExternalId),
    optionalText(entry.senderGroupCard),
  )
}

/**
 * 将快照数字格式化为固定小数位文本。
 *
 * @param value 待格式化的未知值。
 * @param digits 小数位数，默认值为 `0`，必须是非负整数。
 * @returns 格式化后的数字文本；输入不是有限数字时返回 `—`。
 */
export function fixed(value: unknown, digits = 0): string {
  const number = numeric(value)
  return number === null ? '—' : number.toFixed(digits)
}

/**
 * 将分钟数格式化为「x小时y分钟」的中文时长文本。
 *
 * @param totalMinutes 分钟数；负值按 0 处理，用于睡眠倒计时展示。
 * @returns 例如 `3小时5分钟`、`45分钟`、`2小时`。
 */
export function durationCn(totalMinutes: number): string {
  const minutes = Math.max(0, Math.round(totalMinutes))
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  if (hours > 0 && rest > 0) return `${hours}小时${rest}分钟`
  if (hours > 0) return `${hours}小时`
  return `${rest}分钟`
}

/**
 * 将毫秒时间戳格式化为中文月日、时分秒文本。
 *
 * @param value 待格式化的未知时间戳，单位为毫秒。
 * @returns 本地化时间文本；时间戳缺失、非有限或不大于零时返回 `—`。
 */
export function dateTime(value: unknown): string {
  const timestamp = numeric(value)
  if (timestamp === null || timestamp <= 0) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(timestamp))
}

/**
 * 格式化后端阶段停留时长。
 *
 * @param ms 时长，单位为毫秒。
 * @returns 小于 1 秒时使用毫秒，短于 1 分钟时使用秒，否则使用分秒文本。
 */
export function elapsedLabel(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}分${Math.floor((ms % 60_000) / 1000)}秒`
}

/**
 * 将观察 stream 转换为下拉框展示标签。
 *
 * @param stream 后端返回的观察 stream。
 * @returns 包含平台、会话类型、外部标识和内部 ID 的文本标签。
 */
export function streamLabel(stream: ObservabilityStream): string {
  if (stream.kind === 'desktop') return `桌面 · #${stream.id}`
  const kind = stream.kind === 'direct' ? '私聊' : '群聊'
  return `${stream.platform.toUpperCase()} ${kind} · ${stream.externalId} · #${stream.id}`
}

/**
 * 将追踪事件中的消息数组格式化为按角色分段的纯文本。
 *
 * @param messages LLM 消息数组或未知值。
 * @returns 每条消息包含角色和正文的文本；非数组值直接转换为字符串。
 */
export function formatMessages(messages: unknown): string {
  if (!Array.isArray(messages)) return String(messages ?? '')
  return messages.map((item) => {
    const entry = record(item)
    const content = entry.content
    const contentText = typeof content === 'string' ? content : JSON.stringify(content)
    return `[${text(entry.role)}]\n${contentText}`
  }).join('\n\n')
}

/**
 * 删除追踪事件的索引字段并序列化剩余详情。
 *
 * @param entry 单条追踪事件。
 * @returns 不包含序号、时间、类型和轮次的 JSON 文本。
 */
export function traceDetail(entry: TraceEntry): string {
  const { seq: _seq, at: _at, kind: _kind, turnId: _turnId, ...detail } = entry
  return JSON.stringify(detail)
}
