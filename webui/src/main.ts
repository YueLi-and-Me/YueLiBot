/**
 * 构建后端观察 WebUI 的数据加载、状态展示、事件追踪和人物详情交互。
 *
 * 页面通过 HTTP 和 WebSocket 读取 FastAPI 观察接口，将后端快照转换为只读 DOM；
 * 本模块不执行聊天发送、配置保存或其他业务写入。
 */
import type {
  ObservabilityPayload,
  ObservabilityStream,
  ObservabilityStreamsPayload,
  PersonProfile,
  PersonSummary,
  PersonsPayload,
  TraceEntry,
} from '../../electron/shared/ipc.ts'

const SNAPSHOT_REFRESH_MS = 15_000
// 阶段状态使用秒级刷新，以反映后端当前处理阶段。
const STAGE_POLL_MS = 1_000
const MAX_TRACE_ENTRIES = 1_000
const MAX_LOG_ROWS = 500

const loginPanel = document.getElementById('login-panel') as HTMLElement
const panelShell = document.getElementById('panel-shell') as HTMLElement
const loginForm = document.getElementById('login-form') as HTMLFormElement
const loginError = document.getElementById('login-error') as HTMLElement
const streamSelect = document.getElementById('stream-select') as HTMLSelectElement
const stageBoard = document.getElementById('stage-board') as HTMLElement
const conversationView = document.getElementById('conversation-view') as HTMLElement
const personsView = document.getElementById('persons-view') as HTMLElement
const personsGrid = document.getElementById('persons-grid') as HTMLElement
const personsTitle = document.getElementById('persons-title') as HTMLElement
const personsSubtitle = document.getElementById('persons-subtitle') as HTMLElement
const refreshButton = document.getElementById('refresh') as HTMLButtonElement
const autoRefresh = document.getElementById('auto-refresh') as HTMLInputElement
const fetchedAt = document.getElementById('fetched-at') as HTMLTimeElement
const statusStrip = document.getElementById('status') as HTMLElement
const grid = document.getElementById('grid') as HTMLElement
const traceFilter = document.getElementById('trace-filter') as HTMLSelectElement
const traceCount = document.getElementById('trace-count') as HTMLElement
const turnCards = document.getElementById('turn-cards') as HTMLElement
const traceLog = document.getElementById('trace-log') as HTMLElement
const logOutput = document.getElementById('log-output') as HTMLElement
const logStatus = document.getElementById('log-status') as HTMLElement

let initialized = false
let snapshotTimer: ReturnType<typeof setInterval> | null = null
let stageTimer: ReturnType<typeof setInterval> | null = null
let logReconnectTimer: ReturnType<typeof setTimeout> | null = null
let eventReconnectTimer: ReturnType<typeof setTimeout> | null = null
let logSocket: WebSocket | null = null
let eventSocket: WebSocket | null = null
let eventReconnectDelay = 1_000
let lastTraceSeq = 0
let skippedEventCount = 0
let panelRunning = false
let traces: TraceEntry[] = []

/**
 * 将未知快照字段收窄为非数组对象。
 *
 * @param value 待转换的未知值。
 * @returns 输入为非空对象时返回其记录视图，否则返回空记录。
 */
function record(value: unknown): Record<string, unknown> {
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
function numeric(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/**
 * 将快照值转换为适合页面展示的文本。
 *
 * @param value 待展示的未知值。
 * @returns 非空字符串、布尔值或有限数字的文本表示；其他值返回占位符 `—`。
 */
function text(value: unknown): string {
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
function optionalText(value: unknown): string {
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
function qqSenderLabel(
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
function traceSenderLabel(entry: TraceEntry): string {
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
function fixed(value: unknown, digits = 0): string {
  const number = numeric(value)
  return number === null ? '—' : number.toFixed(digits)
}

/**
 * 将毫秒时间戳格式化为中文月日、时分秒文本。
 *
 * @param value 待格式化的未知时间戳，单位为毫秒。
 * @returns 本地化时间文本；时间戳缺失、非有限或不大于零时返回 `—`。
 */
function dateTime(value: unknown): string {
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
 * 创建并挂载一个观察面板分区。
 *
 * @param title 分区标题。
 * @param subtitle 分区标识或补充说明。
 * @param wide 是否使用宽版布局，默认值为 `false`。
 * @param parent 分区父节点，默认追加到主网格。
 * @returns 新分区的内容容器。
 * @throws 传播 DOM 创建或挂载失败产生的异常。
 */
function section(
  title: string,
  subtitle: string,
  wide = false,
  parent: HTMLElement = grid,
): HTMLElement {
  const card = document.createElement('section')
  card.className = wide ? 'panel-card wide' : 'panel-card'
  const heading = document.createElement('header')
  heading.className = 'section-heading'
  const titleElement = document.createElement('h2')
  titleElement.textContent = title
  const subtitleElement = document.createElement('span')
  subtitleElement.className = 'section-subtitle'
  subtitleElement.textContent = subtitle
  heading.append(titleElement, subtitleElement)
  const body = document.createElement('div')
  body.className = 'section-body'
  card.append(heading, body)
  parent.append(card)
  return body
}

/**
 * 向面板分区追加一行键值指标。
 *
 * @param parent 指标行父节点。
 * @param label 指标名称。
 * @param value 指标展示值。
 * @returns 无返回值。
 */
function metric(parent: HTMLElement, label: string, value: string): void {
  const row = document.createElement('div')
  row.className = 'metric-row'
  const name = document.createElement('span')
  name.className = 'metric-name'
  name.textContent = label
  const output = document.createElement('strong')
  output.className = 'metric-value'
  output.textContent = value
  row.append(name, output)
  parent.append(row)
}

/**
 * 向面板分区追加带无障碍标签的进度条。
 *
 * @param parent 进度条父节点。
 * @param value 当前值；渲染时限制在 `0` 到 `max` 之间。
 * @param max 最大值。
 * @param label 进度条无障碍名称。
 * @returns 无返回值。
 */
function progress(parent: HTMLElement, value: number, max: number, label: string): void {
  const element = document.createElement('progress')
  element.className = 'progress'
  element.max = max
  element.value = Math.min(max, Math.max(0, value))
  element.setAttribute('aria-label', label)
  parent.append(element)
}

/**
 * 向面板分区追加标签式键值片段。
 *
 * @param parent 标签父节点。
 * @param label 标签名称。
 * @param value 标签展示值。
 * @returns 无返回值。
 */
function chip(parent: HTMLElement, label: string, value: string): void {
  const item = document.createElement('span')
  item.className = 'chip'
  const name = document.createElement('strong')
  name.textContent = `${label} `
  item.append(name, document.createTextNode(value))
  parent.append(item)
}

/**
 * 渲染顶部状态摘要，包括睡眠、预算、视觉响应和会话人数。
 *
 * @param payload 后端观察快照。
 * @returns 无返回值。
 */
function renderStatus(payload: ObservabilityPayload): void {
  statusStrip.replaceChildren()
  const sleep = record(payload.sleep)
  const impulse = record(payload.impulse)
  const vision = record(record(payload.sensing).visionStats)
  const sleepLabel = sleep.asleep === true ? '睡着' : sleep.drowsy === true ? '犯困' : '清醒'
  const used = numeric(impulse.used) ?? 0
  const remaining = numeric(impulse.remaining) ?? 0
  const values: Array<readonly [string, string]> = [
    ['当前状态', sleepLabel],
    ['今日预算', `${used} / ${used + remaining}`],
    ['视觉响应', `${fixed(vision.looks)} 看 / ${fixed(vision.spoke)} 说`],
    ['会话人物', `${payload.conversation.participants.length} 人`],
  ]
  for (const [label, value] of values) {
    const item = document.createElement('div')
    item.className = 'status-item'
    metric(item, label, value)
    statusStrip.append(item)
  }
}

/**
 * 渲染自身精力指标和进度条。
 *
 * @param payload 后端观察快照。
 * @returns 无返回值。
 */
function renderSelfState(payload: ObservabilityPayload): void {
  const body = section('自身状态', 'selfState')
  const axes = document.createElement('div')
  axes.className = 'axis-list'
  const definitions = [
    ['精力', payload.selfState.energy, 0, 100],
  ] as const
  for (const [label, value, min, max] of definitions) {
    const item = document.createElement('div')
    metric(item, label, fixed(value, 1))
    progress(item, value - min, max - min, label)
    axes.append(item)
  }
  body.append(axes)
}

/**
 * 渲染睡意概率、睡眠判定线、计划时间和睡眠债指标。
 *
 * @param payload 后端观察快照。
 * @returns 无返回值。
 */
function renderSleep(payload: ObservabilityPayload): void {
  const sleep = record(payload.sleep)
  const body = section('睡眠状态', 'sleep')
  const probability = numeric(sleep.probability)
  if (probability !== null) progress(body, probability, 1, '睡意概率')
  const list = document.createElement('div')
  list.className = 'metric-list spaced'
  metric(list, '当前判断', sleep.asleep === true ? '已睡着' : sleep.drowsy === true ? '正在犯困' : sleep.justWoke === true ? '刚醒' : '清醒')
  metric(list, '睡意概率', fixed(sleep.probability, 3))
  metric(list, '睡眠判定线', fixed(sleep.cutoff, 3))
  metric(list, '距计划入睡', `${fixed(sleep.minutesFromBedtime)} 分钟`)
  metric(list, '自然醒目标', dateTime(sleep.naturalWakeTargetAt))
  metric(list, '有效醒来时刻', dateTime(sleep.effectiveWakeAt))
  metric(list, '睡眠债延迟', `${fixed(sleep.sleepDebtDelayMinutes)} 分钟`)
  body.append(list)
}

/**
 * 渲染当天主题、睡眠提示和日程时间线。
 *
 * @param payload 后端观察快照；`schedule` 为空时显示服务不可用状态。
 * @returns 无返回值。
 */
function renderSchedule(payload: ObservabilityPayload): void {
  const body = section('今天的日程', payload.schedule?.date ?? 'schedule', true)
  if (payload.schedule === null) {
    body.textContent = '日程服务当前不可用。'
    return
  }
  const chips = document.createElement('div')
  chips.className = 'chip-row'
  chip(chips, '主题', payload.schedule.theme)
  if (payload.schedule.sleepEnabled) {
    chip(chips, '入睡', payload.schedule.bedtimeHint)
    chip(chips, '醒来', payload.schedule.wakeHint)
  } else {
    chip(chips, '自动睡眠', '已关闭')
  }
  chip(chips, '承接', payload.schedule.carryOver)
  body.append(chips)
  const timeline = document.createElement('ol')
  timeline.className = 'timeline'
  for (const slot of payload.schedule.slots) {
    const item = document.createElement('li')
    item.className = 'timeline-item'
    const heading = document.createElement('strong')
    heading.textContent = `${slot.from} · ${slot.doing}`
    const mood = document.createElement('p')
    mood.className = 'muted'
    mood.textContent = slot.mood
    item.append(heading, mood)
    timeline.append(item)
  }
  body.append(timeline)
}

/**
 * 渲染主动打扰预算、场景剩余额度和兴趣指标。
 *
 * @param payload 后端观察快照。
 * @returns 无返回值。
 * @remarks 场景可用额度扣除固定预留槽位，保持与后端预算语义一致。
 */
function renderBudget(payload: ObservabilityPayload): void {
  const impulse = record(payload.impulse)
  const body = section('打扰预算', 'impulse')
  const used = numeric(impulse.used) ?? 0
  const remaining = numeric(impulse.remaining) ?? 0
  const total = used + remaining
  progress(body, used, Math.max(1, total), '今日主动开口预算')
  const list = document.createElement('div')
  list.className = 'metric-list spaced'
  metric(list, '已用 / 总额', `${used} / ${total}`)
  metric(list, '场景可用', `${Math.max(0, total - 2 - used)} / ${Math.max(0, total - 2)}`)
  metric(list, '连续未回应', fixed(impulse.ignored))
  metric(list, '当前兴趣值', fixed(impulse.interest, 2))
  metric(list, '攒满还需', `${fixed(impulse.minutesToFull)} 分钟`)
  body.append(list)
}

/**
 * 渲染前台活动、静默状态、视觉开关和按原因统计的视觉事件。
 *
 * @param payload 后端观察快照。
 * @returns 无返回值；仅展示后端已聚合的统计值。
 */
function renderSensing(payload: ObservabilityPayload): void {
  const sensing = record(payload.sensing)
  const vision = record(sensing.visionStats)
  const body = section('感知与视觉', 'visionStats')
  const list = document.createElement('div')
  list.className = 'metric-list'
  metric(list, '当前活动', text(sensing.activity))
  metric(list, '活动描述', text(sensing.description))
  metric(list, '持续时间', `${fixed(sensing.minutes)} 分钟`)
  metric(list, '静默场景', text(sensing.silent))
  metric(list, '视觉启用', text(vision.enabled))
  metric(list, '看过 / 开口', `${fixed(vision.looks)} / ${fixed(vision.spoke)}`)
  body.append(list)
  const byReason = record(vision.byReason)
  for (const [reason, count] of Object.entries(byReason)) metric(body, `视觉原因 · ${reason}`, fixed(count))
}

/**
 * 渲染当前会话的工作消息数量和参与人物链接。
 *
 * @param payload 后端观察快照。
 * @returns 无返回值。
 */
function renderConversation(payload: ObservabilityPayload): void {
  const body = section('会话状态', 'conversation', true)
  const summary = document.createElement('div')
  summary.className = 'chip-row'
  chip(summary, '工作消息', `${payload.conversation.workingMessages} 条`)
  chip(summary, '出现人物', `${payload.conversation.participants.length} 人`)
  body.append(summary)
  if (!payload.conversation.participants.length) {
    const empty = document.createElement('p')
    empty.className = 'muted'
    empty.textContent = '这条会话还没有人物发言。'
    body.append(empty)
    return
  }
  const participants = document.createElement('div')
  participants.className = 'person-link-list'
  for (const person of payload.conversation.participants) {
    const link = document.createElement('a')
    link.className = 'person-link'
    link.href = `/persons/${person.id}`
    link.textContent = qqSenderLabel(
      person.displayName,
      person.nickname,
      person.externalId,
      person.groupCard,
    )
    participants.append(link)
  }
  body.append(participants)
}

/**
 * 渲染语音合成开关、模型音色和缓存统计。
 *
 * @param payload 后端观察快照。
 * @returns 无返回值。
 */
function renderVoice(payload: ObservabilityPayload): void {
  const voice = record(payload.voice)
  const cache = record(voice.cache)
  const body = section('语音与缓存', 'voice')
  const list = document.createElement('div')
  list.className = 'metric-list'
  metric(list, '运行状态', voice.enabled === true ? '已启用' : '未启用')
  metric(list, '服务配置', voice.configured === true ? '已配置' : '未配置')
  metric(list, '模型 / 音色', `${text(voice.model)} / ${text(voice.voice)}`)
  metric(list, '缓存命中 / 未命中', `${fixed(voice.cacheHits)} / ${fixed(voice.cacheMisses)}`)
  metric(list, '连续失败', fixed(voice.failures))
  metric(list, '缓存文件', `${fixed(cache.files)} 个`)
  metric(list, '缓存体积', `${fixed((numeric(cache.bytes) ?? 0) / 1024, 1)} KB`)
  body.append(list)
}

/**
 * 清空并按固定顺序渲染观察快照的全部业务分区。
 *
 * @param payload 后端观察快照。
 * @returns 无返回值。
 * @remarks 保留状态条更新顺序，再清理旧网格节点，避免刷新后残留过期内容。
 */
function renderSnapshot(payload: ObservabilityPayload): void {
  renderStatus(payload)
  grid.replaceChildren()
  renderSelfState(payload)
  renderSleep(payload)
  renderSchedule(payload)
  renderBudget(payload)
  renderSensing(payload)
  renderConversation(payload)
  renderVoice(payload)
}

/**
 * 将人物摘要渲染为列表卡片，并提供进入完整人物画像的链接。
 *
 * @param person 后端返回的人物摘要。
 * @returns 无返回值；卡片追加到人物网格。
 * @throws 传播 DOM 创建、属性写入或节点挂载失败产生的异常。
 */
function renderPersonSummary(person: PersonSummary): void {
  const body = section(
    person.displayName,
    person.kind === 'owner' ? '本人' : `联系人 #${person.id}`,
    false,
    personsGrid,
  )
  const metadata = document.createElement('div')
  metadata.className = 'metric-list'
  metric(metadata, '认识时间', dateTime(person.firstSeenAt))
  metric(metadata, '平台身份', `${person.identities.length} 个`)
  metric(metadata, '出现会话', `${person.streams.length} 个`)
  body.append(metadata)
  const identities = document.createElement('div')
  identities.className = 'chip-row spaced'
  // QQ 身份拆分显示昵称和账号，其他平台保留平台名与外部标识，避免不同平台字段混用。
  for (const identity of person.identities) {
    if (identity.platform === 'qq') {
      chip(identities, 'QQ昵称', identity.displayName)
      chip(identities, 'QQ号', identity.externalId)
    } else {
      chip(identities, identity.platform.toUpperCase(), `${identity.displayName} · ${identity.externalId}`)
    }
  }
  if (!person.identities.length) chip(identities, '身份', '尚未绑定平台身份')
  for (const membership of person.groupMemberships) {
    chip(
      identities,
      `QQ群 ${membership.groupExternalId}`,
      membership.groupCard || '未设置群名片',
    )
  }
  body.append(identities)
  const link = document.createElement('a')
  link.className = 'button-link compact-link'
  link.href = `/persons/${person.id}`
  link.textContent = '查看完整画像'
  body.append(link)
}

/**
 * 渲染人物摘要列表或空状态。
 *
 * @param payload 后端返回的人物分页或列表数据。
 * @returns 无返回值。
 */
function renderPersonList(payload: PersonsPayload): void {
  personsTitle.textContent = '人物画像'
  personsSubtitle.textContent = '每个人的身份、关系与事实记忆彼此独立。'
  personsGrid.replaceChildren()
  if (!payload.persons.length) {
    const empty = document.createElement('p')
    empty.className = 'empty'
    empty.textContent = '还没有认识任何人。'
    personsGrid.append(empty)
    return
  }
  for (const person of payload.persons) renderPersonSummary(person)
}

/**
 * 渲染单个人物的关系、身份、会话、记忆和事实详情。
 *
 * @param profile 后端返回的人物完整画像。
 * @returns 无返回值；页面标题、摘要和详情网格会被原地替换。
 * @throws 传播 DOM 创建、属性写入或节点挂载失败产生的异常。
 * @remarks 记忆和事实内容按纯文本写入，避免后端文本被解释为 HTML。
 */
function renderPersonDetail(profile: PersonProfile): void {
  personsTitle.textContent = profile.displayName
  personsSubtitle.textContent = profile.kind === 'owner'
    ? '用户本人的独立画像'
    : `联系人 #${profile.id} 的人物画像`
  personsGrid.replaceChildren()

  const relationship = section('关系状态', 'persona_bond', false, personsGrid)
  const axes = document.createElement('div')
  axes.className = 'axis-list'
  const definitions = [
    ['好感度', profile.bond.intimacy, 0, 100],
  ] as const
  for (const [label, value, min, max] of definitions) {
    const item = document.createElement('div')
    metric(item, label, fixed(value, 1))
    progress(item, value - min, max - min, label)
    axes.append(item)
  }
  relationship.append(axes)

  // 身份、群成员关系和会话流独立展示，便于区分“是谁”“在哪个群”和“出现在哪个出口”。
  const identity = section('身份与会话', 'identities / streams', false, personsGrid)
  const metadata = document.createElement('div')
  metadata.className = 'metric-list'
  metric(metadata, '认识时间', dateTime(profile.firstSeenAt))
  metric(metadata, '画像更新时间', dateTime(profile.bond.updatedAt))
  identity.append(metadata)
  const identityChips = document.createElement('div')
  identityChips.className = 'chip-row spaced'
  for (const item of profile.identities) {
    if (item.platform === 'qq') {
      chip(identityChips, 'QQ昵称', item.displayName)
      chip(identityChips, 'QQ号', item.externalId)
    } else {
      chip(identityChips, item.platform.toUpperCase(), `${item.displayName} · ${item.externalId}`)
    }
  }
  if (!profile.identities.length) chip(identityChips, '身份', '尚未绑定平台身份')
  for (const membership of profile.groupMemberships) {
    chip(
      identityChips,
      `QQ群 ${membership.groupExternalId}`,
      membership.groupCard || '未设置群名片',
    )
  }
  identity.append(identityChips)
  const streamChips = document.createElement('div')
  streamChips.className = 'chip-row'
  for (const stream of profile.streams) chip(streamChips, stream.kind, streamLabel(stream))
  if (!profile.streams.length) chip(streamChips, '会话', '尚未在会话中发言')
  identity.append(streamChips)

  const memory = section('事实记忆', `${profile.facts.length} 条`, true, personsGrid)
  if (!profile.facts.length) {
    const empty = document.createElement('p')
    empty.className = 'muted'
    empty.textContent = '当前没有关于这个人的事实记忆。'
    memory.append(empty)
  } else {
    // 事实内容使用 textContent 写入，冻结状态仅通过行样式标识，不改变事实原文。
    const wrap = document.createElement('div')
    wrap.className = 'table-wrap'
    const table = document.createElement('table')
    const head = document.createElement('thead')
    const headRow = document.createElement('tr')
    for (const label of ['内容', '类型', '保留度', '复习到期', '状态']) {
      const cell = document.createElement('th')
      cell.textContent = label
      headRow.append(cell)
    }
    head.append(headRow)
    const rows = document.createElement('tbody')
    for (const fact of profile.facts) {
      const row = document.createElement('tr')
      if (fact.frozen) row.className = 'frozen'
      for (const value of [
        fact.content,
        fact.kind,
        fact.retention.toFixed(2),
        dateTime(fact.dueAt),
        fact.frozen ? '渐淡' : '清晰',
      ]) {
        const cell = document.createElement('td')
        cell.textContent = value
        row.append(cell)
      }
      rows.append(row)
    }
    table.append(head, rows)
    wrap.append(table)
    memory.append(wrap)
  }

  const back = document.createElement('a')
  back.className = 'button-link compact-link'
  back.href = '/persons'
  back.textContent = '返回人物列表'
  memory.append(back)
}

/**
 * 从当前 URL 解析人物页面路由。
 *
 * @returns `/persons` 时返回 `null`，`/persons/<正整数>` 时返回人物 ID，其他路径返回 `undefined`。
 */
function requestedPersonId(): number | null | undefined {
  const path = window.location.pathname.replace(/\/$/, '') || '/'
  if (path === '/persons') return null
  const match = /^\/persons\/(\d+)$/.exec(path)
  return match ? Number(match[1]) : undefined
}

/**
 * 请求人物列表或指定人物画像并渲染对应页面。
 *
 * @param personId 人物 ID；传入 `null` 请求列表。
 * @returns 请求和渲染完成后的 Promise。
 * @throws Error 当请求返回非成功且非 401/404 状态时抛出；401 和 404 转换为页面状态。
 * @remarks 请求使用同源凭据；401 会切换到登录面板，404 只更新人物区域的错误提示。
 */
async function fetchPersonPage(personId: number | null): Promise<void> {
  const path = personId === null ? '/api/persons' : `/api/persons/${personId}`
  const response = await fetch(path, { credentials: 'same-origin' })
  if (response.status === 401) {
    showLogin('登录已失效，请重新输入 token。')
    return
  }
  if (response.status === 404) {
    personsGrid.replaceChildren()
    const message = document.createElement('p')
    message.className = 'error'
    message.textContent = '找不到这个人物画像。'
    personsGrid.append(message)
    return
  }
  if (!response.ok) throw new Error(`人物画像请求失败：HTTP ${response.status}`)
  if (personId === null) {
    renderPersonList(await response.json() as PersonsPayload)
  } else {
    renderPersonDetail(await response.json() as PersonProfile)
  }
}

/**
 * 将追踪事件中的消息数组格式化为按角色分段的纯文本。
 *
 * @param messages LLM 消息数组或未知值。
 * @returns 每条消息包含角色和正文的文本；非数组值直接转换为字符串。
 */
function formatMessages(messages: unknown): string {
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
function traceDetail(entry: TraceEntry): string {
  const { seq: _seq, at: _at, kind: _kind, turnId: _turnId, ...detail } = entry
  return JSON.stringify(detail)
}

/**
 * 按当前类型和 stream 筛选条件重绘对话轮次卡片及后台事件列表。
 *
 * @returns 无返回值；筛选结果写入追踪区域。
 * @remarks 对话事件按 `turnId` 聚合，后台事件按时间顺序单独展示；每次重绘都会清理旧节点。
 * @throws 传播 DOM 创建、属性写入或节点挂载失败产生的异常。
 */
function renderTrace(): void {
  const selectedKind = traceFilter.value
  const selectedStream = Number(streamSelect.value)
  const visible = traces.filter((entry) => {
    const kindMatches = selectedKind === 'all' || entry.kind === selectedKind
    const streamMatches = entry.streamId == null || entry.streamId === selectedStream
    return kindMatches && streamMatches
  })
  traceCount.textContent = `${visible.length} / ${traces.length} 条${
    skippedEventCount ? ` · 已跳过 ${skippedEventCount} 条` : ''
  }`
  turnCards.replaceChildren()
  traceLog.replaceChildren()

  const grouped = new Map<number, TraceEntry[]>()
  for (const entry of visible) {
    if (entry.turnId == null) continue
    const group = grouped.get(entry.turnId) ?? []
    group.push(entry)
    grouped.set(entry.turnId, group)
  }
  for (const [turnId, entries] of [...grouped.entries()].reverse().slice(0, 30)) {
    const card = document.createElement('article')
    card.className = 'trace-card'
    const title = document.createElement('strong')
    const origin = entries.find((entry) => entry.platform !== undefined)
    title.textContent = `Turn #${turnId} · ${origin?.platform ?? '未知来源'} · stream ${origin?.streamId ?? '—'} · person ${origin?.personId ?? '—'}`
    card.append(title)
    for (const entry of entries) {
      if (entry.kind === 'user_input') {
        const row = document.createElement('p')
        row.textContent = `${traceSenderLabel(entry)}：${text(entry.text)}`
        card.append(row)
      } else if (entry.kind === 'llm_request') {
        const details = document.createElement('details')
        const summary = document.createElement('summary')
        summary.textContent = '发送的 Prompt'
        const pre = document.createElement('pre')
        pre.textContent = formatMessages(entry.messages)
        details.append(summary, pre)
        card.append(details)
      } else if (entry.kind === 'llm_final') {
        const row = document.createElement('p')
        row.className = 'trace-response'
        row.textContent = `${optionalText(entry.botName) || 'Bot'}：${text(entry.text)}`
        card.append(row)
      } else if (entry.kind === 'memory_fact') {
        chip(card, '记忆', `[${text(entry.memoryKind)}] ${text(entry.content)}`)
      } else if (entry.kind === 'mood_delta') {
        chip(card, '心情', `favor=${fixed(entry.favor)} energy=${fixed(entry.energy)}`)
      } else if (entry.kind === 'llm_error') {
        card.classList.add('error-border')
        chip(card, '错误', `${text(entry.errorKind)} · ${text(entry.message)}`)
      }
    }
    turnCards.append(card)
  }

  for (const entry of visible.filter((item) => item.turnId == null).slice(-300)) {
    const row = document.createElement('div')
    row.className = 'trace-entry'
    const time = document.createElement('span')
    time.className = 'muted mono'
    time.textContent = dateTime(entry.at)
    const kind = document.createElement('strong')
    kind.textContent = entry.kind
    const detail = document.createElement('span')
    // 观察事件直接展示原消息和后端门控原因，避免把“未回复”误判为链路故障。
    if (entry.kind === 'observation') {
      row.classList.add('trace-observation')
      detail.textContent = `${traceSenderLabel(entry)}：${text(entry.text)}`
      const why = document.createElement('span')
      why.className = 'muted'
      why.textContent = `未回复：${text(entry.reason)}`
      row.append(time, kind, detail, why)
      traceLog.append(row)
      continue
    }
    detail.textContent = traceDetail(entry)
    row.append(time, kind, detail)
    traceLog.append(row)
  }
  if (!turnCards.children.length) turnCards.textContent = '当前筛选条件下没有对话轮次。'
  if (!traceLog.children.length) traceLog.textContent = '当前筛选条件下没有后台事件。'
}

/**
 * 将 ANSI 256 色索引转换为 CSS 颜色文本。
 *
 * @param index ANSI 颜色索引，通常范围为 0~255。
 * @returns 对应的十六进制或 `rgb()` 颜色文本；超出范围时按算法结果生成颜色。
 */
function ansi256(index: number): string {
  if (index < 16) {
    const colors = ['#000000', '#800000', '#008000', '#808000', '#000080', '#800080', '#008080', '#c0c0c0', '#808080', '#ff0000', '#00ff00', '#ffff00', '#0000ff', '#ff00ff', '#00ffff', '#ffffff']
    return colors[index] ?? '#e8edf5'
  }
  if (index >= 232) {
    const value = 8 + (index - 232) * 10
    return `rgb(${value} ${value} ${value})`
  }
  const offset = index - 16
  const levels = [0, 95, 135, 175, 215, 255]
  return `rgb(${levels[Math.floor(offset / 36)]} ${levels[Math.floor(offset / 6) % 6]} ${levels[offset % 6]})`
}

/**
 * 解析一行 ANSI 转义日志并追加为带颜色和粗体样式的 DOM 行。
 *
 * @param line 含 ANSI SGR 转义序列的日志文本。
 * @returns 无返回值。
 * @remarks 支持基础色、粗体、24 位 RGB 和 256 色；追加后保留最多 {@link MAX_LOG_ROWS} 行并滚动到底部。
 * @throws 传播 DOM 创建或样式写入失败产生的异常。
 */
function appendAnsiLine(line: string): void {
  const row = document.createElement('div')
  row.className = 'log-row'
  let color = ''
  let bold = false
  let cursor = 0
  const pattern = /\x1b\[([0-9;]*)m/g
  // 按 SGR 序列切分文本，并把每个片段绑定到当前颜色和粗体状态。
  for (const match of line.matchAll(pattern)) {
    const index = match.index ?? 0
    if (index > cursor) {
      const span = document.createElement('span')
      span.textContent = line.slice(cursor, index)
      if (color) span.style.color = color
      if (bold) span.style.fontWeight = '700'
      row.append(span)
    }
    const codes = (match[1] ?? '').split(';').filter(Boolean).map(Number)
    if (!codes.length || codes.includes(0)) {
      color = ''
      bold = false
    }
    if (codes.includes(1)) bold = true
    const basic: Record<number, string> = { 31: '#ff6b6b', 33: '#ffd166', 35: '#d787ff' }
    for (const code of codes) if (basic[code]) color = basic[code]
    // 24 位颜色和 256 色都使用前缀 38；分别读取后续模式和值。
    const trueColorAt = codes.indexOf(38)
    if (trueColorAt >= 0 && codes[trueColorAt + 1] === 2) {
      color = `rgb(${codes[trueColorAt + 2]} ${codes[trueColorAt + 3]} ${codes[trueColorAt + 4]})`
    } else if (trueColorAt >= 0 && codes[trueColorAt + 1] === 5) {
      color = ansi256(codes[trueColorAt + 2] ?? 15)
    }
    cursor = index + match[0].length
  }
  if (cursor < line.length) {
    const span = document.createElement('span')
    span.textContent = line.slice(cursor)
    if (color) span.style.color = color
    if (bold) span.style.fontWeight = '700'
    row.append(span)
  }
  logOutput.append(row)
  while (logOutput.children.length > MAX_LOG_ROWS) logOutput.firstElementChild?.remove()
  logOutput.scrollTop = logOutput.scrollHeight
}

/**
 * 建立日志 WebSocket 连接，并在断开后延迟重连。
 *
 * @returns 无返回值；连接状态和接收日志直接更新日志面板。
 * @remarks 单条日志 JSON 解析失败只跳过该条，不关闭当前连接。
 * @throws 传播 WebSocket 构造失败产生的异常。
 */
function connectLogs(): void {
  if (logReconnectTimer) clearTimeout(logReconnectTimer)
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  logSocket = new WebSocket(`${scheme}://${location.host}/ws/logs`)
  logSocket.addEventListener('open', () => {
    logStatus.textContent = '已连接'
  })
  logSocket.addEventListener('message', (event) => {
    try {
      const item = JSON.parse(String(event.data)) as { line?: unknown }
      if (typeof item.line === 'string') appendAnsiLine(item.line)
    } catch {
      // 单条格式损坏只跳过该行，连接会继续接收后续日志。
    }
  })
  logSocket.addEventListener('close', () => {
    logStatus.textContent = '已断开，3 秒后重连'
    logReconnectTimer = setTimeout(connectLogs, 3_000)
  })
}

/**
 * 建立追踪事件 WebSocket 连接，并按序号游标接收增量事件。
 *
 * @returns 无返回值；连接关闭时使用指数退避，最大重连间隔为 30 秒。
 * @remarks 当面板未运行时不创建连接；服务端报告截断时累计被跳过的事件数量。
 * @throws 传播 WebSocket 构造失败产生的异常。
 */
function connectEvents(): void {
  if (!panelRunning) return
  if (eventReconnectTimer) clearTimeout(eventReconnectTimer)
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  eventSocket = new WebSocket(
    `${scheme}://${location.host}/ws/events?since=${encodeURIComponent(lastTraceSeq)}`,
  )
  eventSocket.addEventListener('open', () => {
    eventReconnectDelay = 1_000
  })
  eventSocket.addEventListener('message', (message) => {
    try {
      const payload = JSON.parse(String(message.data)) as {
        events?: unknown
        truncated?: unknown
        from?: unknown
      }
      if (!Array.isArray(payload.events)) return
      if (payload.truncated === true && typeof payload.from === 'number') {
        skippedEventCount += Math.max(0, payload.from - lastTraceSeq - 1)
      }
      const incoming: TraceEntry[] = []
      for (const value of payload.events) {
        const entry = record(value) as TraceEntry
        if (typeof entry.kind !== 'string') continue
        if (typeof entry.seq === 'number') {
          if (entry.seq <= lastTraceSeq) continue
          lastTraceSeq = entry.seq
        }
        incoming.push(entry)
      }
      if (!incoming.length) return
      traces = [...traces, ...incoming].slice(-MAX_TRACE_ENTRIES)
      renderTrace()
    } catch {
      eventSocket?.close()
    }
  })
  eventSocket.addEventListener('close', () => {
    eventSocket = null
    if (!panelRunning) return
    eventReconnectTimer = setTimeout(connectEvents, eventReconnectDelay)
    eventReconnectDelay = Math.min(eventReconnectDelay * 2, 30_000)
  })
}

/**
 * 读取可观察 stream 列表并更新 stream 下拉框。
 *
 * @returns 请求和下拉框更新完成后的 Promise。
 * @throws Error 当未授权、HTTP 请求失败或后端返回空 stream 列表时抛出。
 */
async function fetchStreams(): Promise<void> {
  const response = await fetch('/streams', { credentials: 'same-origin' })
  if (response.status === 401) throw new Error('UNAUTHORIZED')
  if (!response.ok) throw new Error(`stream 列表请求失败：HTTP ${response.status}`)
  const payload = await response.json() as ObservabilityStreamsPayload
  streamSelect.replaceChildren()
  for (const stream of payload.streams) {
    const option = document.createElement('option')
    option.value = String(stream.id)
    option.textContent = streamLabel(stream)
    streamSelect.append(option)
  }
  if (!payload.streams.length) throw new Error('后端没有可观察的 stream')
}

/**
 * 将观察 stream 转换为下拉框展示标签。
 *
 * @param stream 后端返回的观察 stream。
 * @returns 包含平台、会话类型、外部标识和内部 ID 的文本标签。
 */
function streamLabel(stream: ObservabilityStream): string {
  if (stream.kind === 'desktop') return `桌面 · #${stream.id}`
  const kind = stream.kind === 'direct' ? '私聊' : '群聊'
  return `${stream.platform.toUpperCase()} ${kind} · ${stream.externalId} · #${stream.id}`
}

/**
 * 按当前 stream 读取观察快照并刷新业务面板。
 *
 * @returns 请求和渲染完成后的 Promise。
 * @throws 不向上抛出读取或渲染异常；错误会转换为面板中的状态提示。
 * @remarks 请求期间禁用刷新按钮，401 会切换登录面板，完成后恢复按钮状态。
 */
async function fetchSnapshot(): Promise<void> {
  refreshButton.disabled = true
  try {
    const response = await fetch(`/observability?streamId=${encodeURIComponent(streamSelect.value)}`, {
      credentials: 'same-origin',
    })
    if (response.status === 401) {
      showLogin('登录已失效，请重新输入 token。')
      return
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    renderSnapshot(await response.json() as ObservabilityPayload)
    const fetched = new Date()
    fetchedAt.dateTime = fetched.toISOString()
    fetchedAt.textContent = `读取于 ${dateTime(fetched.getTime())}`
  } catch (error) {
    grid.replaceChildren()
    const message = document.createElement('p')
    message.className = 'error'
    message.textContent = `读不到内部状态：${error instanceof Error ? error.message : String(error)}`
    grid.append(message)
  } finally {
    refreshButton.disabled = false
  }
}

interface StageEntry {
  streamId: number
  streamName: string
  stage: string
  stageLabel: string
  detail: string
  turnId: number | null
  stageElapsedMs: number
}

/**
 * 格式化后端阶段停留时长。
 *
 * @param ms 时长，单位为毫秒。
 * @returns 小于 1 秒时使用毫秒，短于 1 分钟时使用秒，否则使用分秒文本。
 */
function elapsedLabel(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}分${Math.floor((ms % 60_000) / 1000)}秒`
}

/**
 * 读取各 stream 当前处理阶段并刷新阶段看板。
 *
 * @returns 一次轮询完成后的 Promise；HTTP 非成功响应时保留现有看板。
 * @throws 传播网络请求、JSON 解析或 DOM 更新错误。
 * @remarks 该方法由秒级定时器调用，保持后端阶段状态的近实时展示。
 */
async function pollStages(): Promise<void> {
  const response = await fetch('/stages', { credentials: 'same-origin' })
  if (!response.ok) return
  const entries = ((await response.json()).stages ?? []) as StageEntry[]
  stageBoard.replaceChildren()
  if (!entries.length) {
    const empty = document.createElement('p')
    empty.className = 'empty'
    empty.textContent = '尚未收到任何消息。'
    stageBoard.append(empty)
    return
  }
  // 每行只展示当前阶段、详情和停留时间，避免把后端内部状态对象直接暴露到页面。
  for (const entry of entries) {
    const row = document.createElement('div')
    row.className = 'stage-row'
    if (entry.stage === 'failed') row.classList.add('error-border')

    const name = document.createElement('strong')
    name.textContent = entry.streamName
    const stage = document.createElement('span')
    stage.className = 'stage-name'
    stage.textContent = entry.stageLabel
    const detail = document.createElement('span')
    detail.className = 'muted'
    detail.textContent = entry.detail
    const elapsed = document.createElement('span')
    elapsed.className = 'muted mono'
    elapsed.textContent = elapsedLabel(entry.stageElapsedMs)

    row.append(name, stage, detail, elapsed)
    if (entry.turnId !== null) {
      const turn = document.createElement('span')
      turn.className = 'muted mono'
      turn.textContent = `Turn #${entry.turnId}`
      row.append(turn)
    }
    stageBoard.append(row)
  }
}

/**
 * 停止观察面板的定时器、WebSocket 和待执行重连任务。
 *
 * @returns 无返回值；重复调用安全。
 */
function stopPanel(): void {
  panelRunning = false
  if (snapshotTimer) clearInterval(snapshotTimer)
  if (stageTimer) clearInterval(stageTimer)
  if (eventReconnectTimer) clearTimeout(eventReconnectTimer)
  snapshotTimer = null
  stageTimer = null
  eventReconnectTimer = null
  eventSocket?.close()
  eventSocket = null
  logSocket?.close()
  logSocket = null
}

/**
 * 显示观察面板并清理登录表单和错误提示。
 *
 * @returns 无返回值。
 */
function showPanel(): void {
  loginForm.reset()
  loginError.textContent = ''
  loginPanel.hidden = true
  panelShell.hidden = false
}

/**
 * 停止面板运行并显示登录区域。
 *
 * @param message 可选登录错误提示，默认值为空字符串。
 * @returns 无返回值。
 */
function showLogin(message = ''): void {
  stopPanel()
  panelShell.hidden = true
  loginPanel.hidden = false
  loginError.textContent = message
}

/**
 * 初始化观察面板，按 URL 选择会话总览或人物画像页面。
 *
 * @returns 初始化请求和连接建立完成后的 Promise。
 * @throws Error 当 stream、快照或人物接口请求失败时抛出，由登录入口统一转换为错误提示。
 * @remarks 方法只注册一次静态交互监听器，后续调用复用既有监听器并重新建立运行期连接。
 */
async function initializePanel(): Promise<void> {
  panelRunning = true
  showPanel()
  const personId = requestedPersonId()
  if (personId !== undefined) {
    // 人物路由不需要启动会话轮询，先停止旧面板连接再加载人物页面。
    stopPanel()
    conversationView.hidden = true
    personsView.hidden = false
    await fetchPersonPage(personId)
    return
  }
  conversationView.hidden = false
  personsView.hidden = true
  // 会话总览先加载 stream 和快照，再连接增量事件与日志通道。
  await fetchStreams()
  await fetchSnapshot()
  renderTrace()
  if (!initialized) {
    // 静态控件只绑定一次；后续重新认证或切换页面时复用同一组监听器。
    initialized = true
    streamSelect.addEventListener('change', () => {
      renderTrace()
      void fetchSnapshot()
    })
    refreshButton.addEventListener('click', () => void fetchSnapshot())
    traceFilter.addEventListener('change', renderTrace)
    autoRefresh.addEventListener('change', () => {
      if (snapshotTimer) clearInterval(snapshotTimer)
      snapshotTimer = autoRefresh.checked
        ? setInterval(() => void fetchSnapshot(), SNAPSHOT_REFRESH_MS)
        : null
    })
  }
  // 阶段、事件和日志连接属于运行期资源，面板停止时由 stopPanel 统一释放。
  stageTimer = setInterval(() => void pollStages(), STAGE_POLL_MS)
  void pollStages()
  connectEvents()
  connectLogs()
}

/**
 * 检查当前浏览器会话是否已通过认证，并在认证成功后初始化面板。
 *
 * @returns 会话检查和可选面板初始化完成后的 Promise。
 * @throws Error 当会话接口返回非成功状态或响应无法解析时抛出。
 */
async function checkSession(): Promise<void> {
  const response = await fetch('/auth/session', { credentials: 'same-origin' })
  if (!response.ok) throw new Error(`会话状态请求失败：HTTP ${response.status}`)
  const state = await response.json() as { authenticated: boolean }
  if (state.authenticated) await initializePanel()
}

loginForm.addEventListener('submit', async (event) => {
  event.preventDefault()
  loginError.textContent = ''
  const formData = new FormData(loginForm)
  const response = await fetch('/auth/login', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: formData.get('token') }),
  })
  loginForm.reset()
  if (!response.ok) {
    showLogin(response.status === 401 ? 'token 不正确，请重新输入。' : '登录失败，请查看后端日志。')
    return
  }
  await initializePanel()
})

void checkSession().catch(() => showLogin('连接不到后端，请确认服务已启动。'))
