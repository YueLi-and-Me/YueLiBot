/**
 * 观察面板的数据收窄与展示格式化工具。
 *
 * 后端快照字段为弱类型 JSON，本模块提供 record/numeric/text 等收窄函数，以及
 * 中文时长、时间戳、发送者标签等展示文本的格式化；被 features 下全部业务
 * 组件依赖。逻辑移植自旧版渲染入口，行为保持一致。
 */
import type { ObservabilityStream, TraceEntry } from '../../../electron/shared/ipc.ts'

/** 事件协议名与观察面板中文名称；协议值保留英文，仅展示时查表。 */
const TRACE_KIND_LABELS: Record<string, string> = {
  action_decision: '行动决策',
  delivery_failed: '投递失败',
  emoji_banned: '表情包封禁',
  emoji_cleanup: '表情包孤儿清理',
  emoji_evicted: '表情包淘汰',
  emoji_registration_rejected: '表情包拒绝入库',
  emoji_selected: '选择表情包',
  emoji_selection_missed: '表情包未命中库',
  expression_select: '表达方式选择',
  expression_learned: '表达方式学习完成',
  expression_learn_failed: '表达方式学习失败',
  foreground: '前台活动变化',
  image_description: '图片理解',
  interest: '兴趣度更新',
  jargon_hit: '黑话命中',
  jargon_inferred: '黑话推断',
  jargon_mined: '黑话提取',
  llm_chunk: '模型流式片段',
  llm_error: '模型调用失败',
  llm_final: '模型输出完成',
  llm_request: '请求模型',
  memory_conflict_resolved: '记忆冲突裁决',
  memory_fact: '写入记忆',
  memory_fact_invalidated: '记忆事实标失效',
  memory_fact_pinned: '记忆事实永久保留',
  memory_fact_replaced: '记忆事实人工取代',
  memory_fact_restored: '记忆事实恢复',
  memory_fact_unpinned: '记忆事实取消永久保留',
  memory_operation_undone: '记忆操作撤销',
  memory_retrieval_trace: '事实召回留痕',
  mood_delta: '心情变化',
  observation: '旁听消息',
  outbound_delivered: '回复投递完成',
  outbound_dropped: '出站消息已丢弃',
  proactive_decision: '主动发言决策',
  proactive_intent: '主动意图评估',
  promise_rejected_for_person: '约定人物归属不匹配',
  promise_stashed: '约定已登记',
  reply_gate: '回复门控判定',
  sleep_transition: '睡眠状态变化',
  stage: '处理阶段变化',
  tool_execution: '工具执行',
  turn_action: '轮次动作规划',
  turn_competition: '轮次竞争处理',
  user_input: '收到用户消息',
  vision_glance: '视觉扫视',
}

/** 追踪载荷字段与中文标签；未知扩展字段仍原样显示。 */
const TRACE_FIELD_LABELS: Record<string, string> = {
  accepted: '已接收',
  action: '动作',
  activeSource: '生效来源',
  activity: '活动类型',
  ageMs: '数据年龄',
  agentScope: '决策模式',
  allow: '允许执行',
  app: '应用',
  asleep: '已入睡',
  availableActions: '可用动作',
  bareMeaning: '只看词含义',
  blockedSource: '被拦来源',
  botName: '机器人',
  botNames: '机器人名称',
  bytes: '字节数',
  candidateCount: '候选数量',
  candidatePool: '候选池',
  candidates: '候选数量',
  channel: '通道',
  content: '内容',
  conversationImpression: '会话印象检索词',
  contextMeaning: '语境含义',
  dropped: '丢弃数量',
  count: '数量',
  currentText: '当前文本检索词',
  currentTextChars: '当前文本字数',
  decisionSource: '决策来源',
  decision: '最终决策',
  detail: '说明',
  disposition: '门控结果',
  drowsy: '正在犯困',
  elapsedMs: '耗时',
  energy: '精力变化',
  error: '错误',
  errorKind: '错误类型',
  errorType: '错误类型',
  enabled: '已启用',
  eventStatus: '事件状态',
  externalMessageId: '外部消息编号',
  favor: '好感变化',
  factId: '事实编号',
  gateDisposition: '门控结果',
  gateReasonCodes: '门控理由',
  gate: '门控信息',
  habits: '表达习惯数量',
  hash: '内容指纹',
  impressionChars: '会话印象字数',
  interest: '兴趣度',
  intensity: '活动强度',
  intentType: '意图类型',
  inputs: '输入条件',
  latencyMs: '模型耗时',
  length: '回复篇幅',
  memoryKind: '记忆类型',
  maxRepliesInWindow: '窗口回复上限',
  maxTokens: '最大令牌数',
  message: '错误信息',
  messageCount: '消息条数',
  messages: '提示词消息',
  messageWatermark: '消息水位',
  mentionedMe: '提到机器人',
  modelName: '模型',
  modelTask: '模型任务',
  nameMentioned: '叫到机器人名字',
  naturalReplyElapsedMs: '自然接话间隔',
  personId: '人物编号',
  personKind: '人物类型',
  platform: '平台',
  probability: '概率',
  process: '进程',
  promptHash: '提示词指纹',
  promptId: '提示词模板',
  promptFactIds: '进提示词的事实编号',
  providerName: '模型服务',
  quoteMessageId: '引用消息',
  reason: '原因',
  reasonCodes: '决策理由',
  reasons: '丢弃理由',
  reasoning: '模型思考',
  renderParams: '渲染参数',
  reply: '回复内容',
  result: '结果',
  repliesInWindow: '窗口内回复数',
  sightings: '出现次数',
  step: '推断步骤',
  substringHits: '子串命中数',
  scene: '场景变化',
  score: '分数',
  seconds: '秒数',
  senderDisplayName: '发送者显示名',
  senderExternalId: '发送者账号',
  senderGroupCard: '发送者群名片',
  senderLabel: '发送者',
  senderNickname: '发送者昵称',
  sourceLabel: '会话来源',
  silent: '静默场景',
  snapshotPath: '快照路径',
  source: '来源',
  snapshotId: '快照编号',
  stage: '处理阶段',
  stageLabel: '处理阶段',
  streamId: '会话编号',
  streamName: '会话',
  streamExternalId: '会话外部编号',
  streamKind: '会话类型',
  factOriginKind: '事实来源',
  blocked: '被挡数量',
  subject: '约定内容',
  targetMessageIds: '目标消息',
  temperature: '生成温度',
  term: '词条',
  text: '正文',
  textChars: '正文字符数',
  userMessages: '他人消息数',
  version: '版本信息',
  waitedSeconds: '等待秒数',
  windowChanged: '窗口已切换',
  emojiEmotions: '表情情绪',
  expressionIntent: '表达意图',
  learned: '新学数量',
  discarded: '丢弃数量',
  eliminated: '淘汰数量',
  cursor: '游标',
  remaining: '剩余数量',
}

/** 常见协议枚举的中文值；只转换完全匹配项，不改写用户正文。 */
const DISPLAY_VALUE_LABELS: Record<string, string> = {
  assistant: '机器人',
  attention_filtered: '未进入注意范围',
  auto: '系统自动',
  away: '暂时离开',
  backfill: '历史补全',
  billing: '余额不足',
  bot_sleeping: '机器人正在睡觉',
  brief: '简短',
  browsing: '浏览网页',
  busy: '忙碌',
  cached: '使用缓存',
  chat: '聊天',
  coding: '编写代码',
  committed: '已决定执行',
  contact: '联系人',
  delivery_failed: '投递失败',
  desktop: '桌面',
  direct: '私聊',
  direct_question: '明确提问',
  direct_emoji_like: '有人给她的消息贴了表情回应',
  directly_addressed: '直接叫到机器人',
  disabled: '未启用',
  drop: '拦截',
  emotional_support: '需要情绪支持',
  empty: '无结果',
  expire: '到期清理',
  failed: '失败',
  files: '浏览文件',
  flush: '批量写入',
  gate_dropped: '门控拦截',
  gaming: '玩游戏',
  group: '群聊',
  idle: '空闲',
  illegal_action: '动作不合法',
  light: '轻度活动',
  long: '详细',
  manual: '人工',
  model: '模型判定',
  music: '听音乐',
  n4: '自动纠错',
  natural_reply_window: '处于自然接话窗口',
  no_new_value: '没有新的回复价值',
  none: '无',
  ok: '成功',
  others_conversation: '他人之间的对话',
  owner: '主人',
  other: '其他活动',
  parse_error: '解析失败',
  provider_error: '模型服务错误',
  rate_limited: '触发频率限制',
  react: '发表情回应',
  reading: '阅读',
  reply: '回复',
  shadow: '影子观察',
  silent: '保持沉默',
  silent_by_choice: '主动选择沉默',
  skipped: '已跳过',
  stash: '暂存',
  system: '系统',
  timeout: '超时',
  topic_closed: '话题已经结束',
  topic_continuation: '延续当前话题',
  unknown: '未知错误',
  user: '用户',
  video: '观看视频',
  work: '处理工作',
  would_interrupt: '回复会打断交流',
}

/** 面板可直接渲染的一项中文追踪详情。 */
export interface TraceDetailItem {
  label: string
  value: string
  rawKey: string
}

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
 * 读取后端纯函数生成的会话来源标签。
 *
 * @param entry 单条追踪事件。
 * @returns 可直接展示的来源标签；历史事件没有该字段时返回空字符串。
 */
export function traceSourceLabel(entry: TraceEntry): string {
  return optionalText(entry.sourceLabel)
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
  const name = stream.kind === 'group'
    ? stream.displayName.trim() || stream.externalId
    : stream.externalId
  return `${stream.platform.toUpperCase()} ${kind} · ${name} · #${stream.id}`
}

/**
 * 将事件协议名转换成中文显示名。
 *
 * @param kind 稳定的后端事件类型。
 * @returns 已登记类型的中文名；未知类型保留原值，便于发现新协议。
 */
export function traceKindLabel(kind: string): string {
  return TRACE_KIND_LABELS[kind] ?? kind
}

/**
 * 将用户输入的中文事件名或协议名转换为后端检索值。
 *
 * @param value 单个事件类型筛选词。
 * @returns 对应的稳定协议名；未知值原样返回。
 */
export function traceKindQueryValue(value: string): string {
  const matched = Object.entries(TRACE_KIND_LABELS).find(([, label]) => label === value)
  return matched?.[0] ?? value
}

/**
 * 将快照或追踪字段值转换为简体中文显示文本。
 *
 * @param value 任意 JSON 值。
 * @returns 布尔值、枚举、数组和对象的紧凑中文文本。
 */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '—'
  if (typeof value === 'string') return DISPLAY_VALUE_LABELS[value] ?? value
  if (Array.isArray(value)) {
    return value.length ? value.map(displayValue).join('、') : '无'
  }
  const values = record(value)
  return Object.entries(values)
    .map(([key, item]) => `${TRACE_FIELD_LABELS[key] ?? key}：${displayValue(item)}`)
    .join('，') || '无'
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
    return `【${displayValue(entry.role)}】\n${contentText}`
  }).join('\n\n')
}

/**
 * 删除追踪事件的索引字段并生成中文详情项。
 *
 * @param entry 单条追踪事件。
 * @returns 不包含序号、时间、类型和轮次的中文标签/值数组。
 * @remarks 原始发送者字段在已有 senderLabel 时折叠，阶段 ID 在已有中文阶段名时
 * 折叠；消息数组只显示条数，完整提示词由专用展开区呈现。
 */
export function traceDetailItems(entry: TraceEntry): TraceDetailItem[] {
  // 来源标签由轮次头部或后台事件头部单独呈现，详情区不再重复一遍。
  const excluded = new Set(['seq', 'at', 'kind', 'turnId', 'sourceLabel'])
  if (optionalText(entry.senderLabel)) {
    excluded.add('senderDisplayName')
    excluded.add('senderExternalId')
    excluded.add('senderGroupCard')
    excluded.add('senderNickname')
  }
  if (optionalText(entry.stageLabel)) excluded.add('stage')

  return Object.entries(entry)
    .filter(([key, value]) => !excluded.has(key) && value !== null && value !== undefined && value !== '')
    .flatMap(([key, value]) => {
      const label = TRACE_FIELD_LABELS[key] ?? key
      if (key === 'messages' && Array.isArray(value)) {
        return [{ rawKey: key, label, value: `${value.length} 条（可在对话轮次中展开）` }]
      }
      const nested = record(value)
      if (Object.keys(nested).length) {
        return Object.entries(nested).map(([nestedKey, nestedValue]) => ({
          rawKey: `${key}.${nestedKey}`,
          label: `${label} · ${TRACE_FIELD_LABELS[nestedKey] ?? nestedKey}`,
          value: displayValue(nestedValue),
        }))
      }
      return [{ rawKey: key, label, value: displayValue(value) }]
    })
}
