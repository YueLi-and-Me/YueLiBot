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
// 阶段状态需要秒级刷新。
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

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function numeric(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function text(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return '—'
}

function optionalText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

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

function fixed(value: unknown, digits = 0): string {
  const number = numeric(value)
  return number === null ? '—' : number.toFixed(digits)
}

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

function progress(parent: HTMLElement, value: number, max: number, label: string): void {
  const element = document.createElement('progress')
  element.className = 'progress'
  element.max = max
  element.value = Math.min(max, Math.max(0, value))
  element.setAttribute('aria-label', label)
  parent.append(element)
}

function chip(parent: HTMLElement, label: string, value: string): void {
  const item = document.createElement('span')
  item.className = 'chip'
  const name = document.createElement('strong')
  name.textContent = `${label} `
  item.append(name, document.createTextNode(value))
  parent.append(item)
}

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

function requestedPersonId(): number | null | undefined {
  const path = window.location.pathname.replace(/\/$/, '') || '/'
  if (path === '/persons') return null
  const match = /^\/persons\/(\d+)$/.exec(path)
  return match ? Number(match[1]) : undefined
}

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

/** 把 messages 数组摊成 [role] + 正文的分段文本，直接 JSON 化会把提示词里的换行全转义掉。 */
function formatMessages(messages: unknown): string {
  if (!Array.isArray(messages)) return String(messages ?? '')
  return messages.map((item) => {
    const entry = record(item)
    const content = entry.content
    const contentText = typeof content === 'string' ? content : JSON.stringify(content)
    return `[${text(entry.role)}]\n${contentText}`
  }).join('\n\n')
}

function traceDetail(entry: TraceEntry): string {
  const { seq: _seq, at: _at, kind: _kind, turnId: _turnId, ...detail } = entry
  return JSON.stringify(detail)
}

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
    // 直接显示静默群消息及门控原因。
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

function appendAnsiLine(line: string): void {
  const row = document.createElement('div')
  row.className = 'log-row'
  let color = ''
  let bold = false
  let cursor = 0
  const pattern = /\x1b\[([0-9;]*)m/g
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

function streamLabel(stream: ObservabilityStream): string {
  if (stream.kind === 'desktop') return `桌面 · #${stream.id}`
  const kind = stream.kind === 'direct' ? '私聊' : '群聊'
  return `${stream.platform.toUpperCase()} ${kind} · ${stream.externalId} · #${stream.id}`
}

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

/** 格式化阶段停留时长。 */
function elapsedLabel(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}分${Math.floor((ms % 60_000) / 1000)}秒`
}

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

function showPanel(): void {
  loginForm.reset()
  loginError.textContent = ''
  loginPanel.hidden = true
  panelShell.hidden = false
}

function showLogin(message = ''): void {
  stopPanel()
  panelShell.hidden = true
  loginPanel.hidden = false
  loginError.textContent = message
}

async function initializePanel(): Promise<void> {
  panelRunning = true
  showPanel()
  const personId = requestedPersonId()
  if (personId !== undefined) {
    stopPanel()
    conversationView.hidden = true
    personsView.hidden = false
    await fetchPersonPage(personId)
    return
  }
  conversationView.hidden = false
  personsView.hidden = true
  await fetchStreams()
  await fetchSnapshot()
  renderTrace()
  if (!initialized) {
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
  stageTimer = setInterval(() => void pollStages(), STAGE_POLL_MS)
  void pollStages()
  connectEvents()
  connectLogs()
}

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
