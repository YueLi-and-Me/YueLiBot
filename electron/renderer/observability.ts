import type { ObservabilityPayload } from '../shared/ipc.ts'

/** 开发者观察面板：把只读快照翻译成可扫描的业务视图，不展示原始 JSON。 */

const AUTO_REFRESH_MS = 15_000
const grid = document.getElementById('grid') as HTMLElement
const status = document.getElementById('status') as HTMLElement
const refreshButton = document.getElementById('refresh') as HTMLButtonElement
const openSettingsButton = document.getElementById('open-settings') as HTMLButtonElement
const autoRefresh = document.getElementById('auto-refresh') as HTMLInputElement
const fetchedAt = document.getElementById('fetched-at') as HTMLTimeElement
const traceLog = document.getElementById('trace-log') as HTMLElement
const traceCount = document.getElementById('trace-count') as HTMLElement
const turnCardsEl = document.getElementById('turn-cards') as HTMLElement

let fetchCount = 0
let autoRefreshTimer: ReturnType<typeof setInterval> | null = null

const TRACE_POLL_MS = 2_000
const MAX_TRACE_ROWS = 300
const MAX_TURN_CARDS = 30
let lastTraceSeq = 0
let traceTotal = 0

interface TurnCard {
  cardEl: HTMLElement
  userEl: HTMLElement
  statusEl: HTMLElement
  promptBodyEl: HTMLElement
  responseEl: HTMLElement
  effectsEl: HTMLElement
  chunkCount: number
  firstAt: number | null
}
const turnCards = new Map<number, TurnCard>()
const turnOrder: number[] = []

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
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
    hour12: false,
  }).format(new Date(timestamp))
}

function section(title: string, subtitle: string, wide = false): { card: HTMLElement; body: HTMLElement } {
  const card = document.createElement('section')
  card.className = wide ? 'panel-card wide' : 'panel-card'
  const heading = document.createElement('header')
  heading.className = 'section-heading'
  const titleWrap = document.createElement('div')
  titleWrap.className = 'section-title'
  const titleElement = document.createElement('h2')
  titleElement.textContent = title
  titleWrap.append(titleElement)
  const subtitleElement = document.createElement('span')
  subtitleElement.className = 'section-subtitle'
  subtitleElement.textContent = subtitle
  heading.append(titleWrap, subtitleElement)
  const body = document.createElement('div')
  body.className = 'section-body'
  card.append(heading, body)
  grid.append(card)
  return { card, body }
}

function metric(parent: HTMLElement, label: string, value: string): void {
  const row = document.createElement('div')
  row.className = 'metric-row'
  const name = document.createElement('span')
  name.className = 'metric-name'
  name.textContent = label
  const result = document.createElement('strong')
  result.className = 'metric-value'
  result.textContent = value
  row.append(name, result)
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
  const strong = document.createElement('strong')
  strong.textContent = `${label} `
  item.append(strong, document.createTextNode(value))
  parent.append(item)
}

function renderStatus(payload: ObservabilityPayload): void {
  status.replaceChildren()
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
    ['长期记忆', `${payload.memory.semantic.length} 条`],
  ]
  for (const [label, value] of values) {
    const item = document.createElement('div')
    item.className = 'status-item'
    const name = document.createElement('span')
    name.className = 'status-label'
    name.textContent = label
    const output = document.createElement('strong')
    output.className = 'status-value'
    output.textContent = value
    item.append(name, output)
    status.append(item)
  }
}

function renderPersona(payload: ObservabilityPayload): void {
  const { body } = section('人格状态', 'persona')
  const description = document.createElement('p')
  description.className = 'persona-copy'
  description.textContent = payload.persona.description
  body.append(description)

  const axes = document.createElement('div')
  axes.className = 'axis-list'
  const definitions = [
    ['亲密度', payload.persona.state.intimacy, 0, 100, '关系距离'],
    ['傲娇度', payload.persona.state.tsundere, -50, 50, '表达的拐弯程度'],
    ['依赖度', payload.persona.state.reliance, 0, 100, '主动靠近的倾向'],
    ['精力', payload.persona.state.energy, 0, 100, '当前行动余量'],
  ] as const
  for (const [label, value, min, max, hint] of definitions) {
    const item = document.createElement('div')
    const labelRow = document.createElement('div')
    labelRow.className = 'axis-label'
    const name = document.createElement('span')
    name.className = 'axis-name'
    name.textContent = `${label} · ${hint}`
    const output = document.createElement('strong')
    output.className = 'axis-value'
    output.textContent = String(value)
    labelRow.append(name, output)
    item.append(labelRow)
    progress(item, value - min, max - min, label)
    axes.append(item)
  }
  body.append(axes)
}

function renderSchedule(payload: ObservabilityPayload): void {
  const { body } = section('今天的日程', payload.schedule.date, true)
  const chips = document.createElement('div')
  chips.className = 'chip-row'
  chip(chips, '主题', payload.schedule.theme)
  chip(chips, '入睡', payload.schedule.bedtimeHint)
  chip(chips, '醒来', payload.schedule.wakeHint)
  chip(chips, '承接', payload.schedule.carryOver)
  body.append(chips)

  // 只格式化主进程注入的业务时间，不在渲染层读取系统时钟或推导睡眠状态。
  const timeParts = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false })
    .formatToParts(payload.now)
  const currentHour = Number(timeParts.find((part) => part.type === 'hour')?.value ?? 0)
  const currentMinute = Number(timeParts.find((part) => part.type === 'minute')?.value ?? 0)
  const currentMinutes = currentHour * 60 + currentMinute
  let currentIndex = 0
  payload.schedule.slots.forEach((slot: any, index: number) => {
    const [hour, minute] = slot.from.split(':').map(Number)
    if (Number.isFinite(hour) && Number.isFinite(minute) && hour! * 60 + minute! <= currentMinutes) currentIndex = index
  })

  const timeline = document.createElement('ol')
  timeline.className = 'timeline'
  payload.schedule.slots.forEach((slot: any, index: number) => {
    const item = document.createElement('li')
    item.className = index === currentIndex ? 'timeline-item current' : 'timeline-item'
    const heading = document.createElement('div')
    heading.className = 'timeline-heading'
    const time = document.createElement('span')
    time.className = 'timeline-time'
    time.textContent = slot.from
    const doing = document.createElement('strong')
    doing.textContent = slot.doing
    heading.append(time, doing)
    const mood = document.createElement('p')
    mood.className = 'timeline-mood'
    mood.textContent = slot.mood
    item.append(heading, mood)
    timeline.append(item)
  })
  body.append(timeline)
}

function renderSleep(payload: ObservabilityPayload): void {
  const sleep = record(payload.sleep)
  const { body } = section('睡眠状态', 'sleep')
  const probability = numeric(sleep.probability)
  if (probability !== null) {
    const label = document.createElement('div')
    label.className = 'axis-label'
    const name = document.createElement('span')
    name.className = 'axis-name'
    name.textContent = '睡意概率'
    const value = document.createElement('strong')
    value.className = 'axis-value'
    value.textContent = probability.toFixed(3)
    label.append(name, value)
    body.append(label)
    progress(body, probability, 1, '睡意概率')
  }
  const list = document.createElement('div')
  list.className = 'metric-list'
  metric(list, '当前判断', sleep.asleep === true ? '已睡着' : sleep.drowsy === true ? '正在犯困' : sleep.justWoke === true ? '刚醒' : '清醒')
  metric(list, '睡眠判定线', fixed(sleep.cutoff, 3))
  metric(list, '距计划入睡', `${fixed(sleep.minutesFromBedtime)} 分钟`)
  metric(list, '自然醒目标', dateTime(sleep.naturalWakeTargetAt))
  metric(list, '有效醒来时刻', dateTime(sleep.effectiveWakeAt))
  metric(list, '睡眠债延迟', `${fixed(sleep.sleepDebtDelayMinutes)} 分钟`)
  body.append(list)
}

// 后端把预算和冲动放在同一个 impulse 块里（proactive.py 的 observability_fields）。
// 场景类意图只能用到总额减去保留槽的那部分，用完之后她整天都不会再因为
// 切窗口开口——面板必须把这一段单独标出来，否则「还剩 2 额度却再也不说话」
// 会被当成故障来查。
const SCENE_RESERVED_SLOTS = 2

function renderBudget(payload: ObservabilityPayload): void {
  const impulse = record(payload.impulse)
  const { body } = section('打扰预算', 'impulse')
  const used = numeric(impulse.used) ?? 0
  const remaining = numeric(impulse.remaining) ?? 0
  const total = used + remaining
  progress(body, used, Math.max(1, total), '今日主动开口预算')
  const sceneTotal = Math.max(0, total - SCENE_RESERVED_SLOTS)
  const list = document.createElement('div')
  list.className = 'metric-list spaced'
  metric(list, '已用 / 总额', `${used} / ${total}`)
  metric(list, '场景可用', `${Math.max(0, sceneTotal - used)} / ${sceneTotal}`)
  metric(list, '连续未回应', fixed(impulse.ignored))
  metric(list, '当前兴趣值', fixed(impulse.interest, 2))
  metric(list, '攒满还需', `${fixed(impulse.minutesToFull)} 分钟`)
  body.append(list)
}

function renderSensing(payload: ObservabilityPayload): void {
  const sensing = record(payload.sensing)
  const vision = record(sensing.visionStats)
  const byReason = record(vision.byReason)
  const { body } = section('感知与视觉', 'visionStats')
  const list = document.createElement('div')
  list.className = 'metric-list'
  metric(list, '当前活动', text(sensing.activity))
  metric(list, '活动描述', text(sensing.description))
  metric(list, '持续时间', `${fixed(sensing.minutes)} 分钟`)
  metric(list, '静默场景', text(sensing.silent))
  metric(list, '视觉启用', text(vision.enabled))
  metric(list, '看过 / 开口', `${fixed(vision.looks)} / ${fixed(vision.spoke)}`)
  body.append(list)

  const entries = Object.entries(byReason).filter((entry): entry is [string, number] => numeric(entry[1]) !== null)
  if (entries.length) {
    const reasons = document.createElement('div')
    reasons.className = 'reason-list'
    const max = Math.max(1, ...entries.map(([, value]) => value))
    for (const [reason, value] of entries) {
      const item = document.createElement('div')
      const label = document.createElement('div')
      label.className = 'axis-label'
      const name = document.createElement('span')
      name.className = 'axis-name'
      name.textContent = reason
      const output = document.createElement('strong')
      output.className = 'axis-value'
      output.textContent = String(value)
      label.append(name, output)
      item.append(label)
      progress(item, value, max, `视觉原因 ${reason}`)
      reasons.append(item)
    }
    body.append(reasons)
  }
}

function renderMemory(payload: ObservabilityPayload): void {
  const { body } = section('记忆存储', 'L3 / L2 / L1', true)
  const summary = document.createElement('div')
  summary.className = 'memory-summary'
  chip(summary, 'L3 长期事实', `${payload.memory.semantic.length} 条`)
  chip(summary, 'L2 情节', `${payload.memory.episodes} 条`)
  chip(summary, 'L1 工作消息', `${payload.memory.workingMessages} 条`)
  body.append(summary)
  if (!payload.memory.semantic.length) {
    const empty = document.createElement('p')
    empty.className = 'muted'
    empty.textContent = '当前没有长期事实记忆。'
    body.append(empty)
    return
  }

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
  const bodyRows = document.createElement('tbody')
  for (const fact of payload.memory.semantic) {
    const row = document.createElement('tr')
    if (fact.frozen) row.className = 'frozen'
    const values = [fact.content, fact.kind, fact.retention.toFixed(2), dateTime(fact.dueAt), fact.frozen ? '渐淡' : '清晰']
    values.forEach((value, index) => {
      const cell = document.createElement('td')
      if (index > 1) cell.className = 'mono'
      cell.textContent = value
      row.append(cell)
    })
    bodyRows.append(row)
  }
  table.append(head, bodyRows)
  wrap.append(table)
  body.append(wrap)
}

function renderVoice(payload: ObservabilityPayload): void {
  const voice = record(payload.voice)
  const cache = record(voice.cache)
  const { body } = section('语音与缓存', 'voice')
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

function render(payload: ObservabilityPayload): void {
  renderStatus(payload)
  grid.replaceChildren()
  renderPersona(payload)
  renderSleep(payload)
  renderSchedule(payload)
  renderBudget(payload)
  renderSensing(payload)
  renderMemory(payload)
  renderVoice(payload)
}

/**
 * 调试追踪：按对话轮次（turnId）分组成卡片——用户说了什么 / 发送的完整
 * Prompt（可展开）/ 她回了什么 / 记忆与心情变化 / 是否出错，而不是一行行
 * 原始 JSON。没有 turnId 的（感知/睡眠这类后台事件）走旁边的轻量列表，
 * 它们没有天然的分组键，不强行卡片化。
 *
 * 新卡片插到容器顶部而不是滚动到底部——读旧卡片时不会被新卡片从底下顶跑。
 */

function traceTime(at: unknown): string {
  const ts = numeric(at)
  if (ts === null) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(new Date(ts))
}

function appendUngroupedEntry(entry: Record<string, unknown>): void {
  if (traceLog.querySelector('.empty')) traceLog.replaceChildren()
  const { seq: _seq, at: _at, kind: _kind, turnId: _turnId, ...rest } = entry
  const row = document.createElement('div')
  row.className = 'trace-entry'
  const time = document.createElement('span')
  time.className = 'trace-time'
  time.textContent = traceTime(entry.at)
  const kind = document.createElement('span')
  kind.className = 'trace-kind'
  kind.textContent = String(entry.kind ?? '?')
  const detail = document.createElement('span')
  detail.className = 'trace-detail'
  detail.textContent = JSON.stringify(rest)
  row.append(time, kind, detail)
  traceLog.append(row)
  while (traceLog.children.length > MAX_TRACE_ROWS) traceLog.firstElementChild?.remove()
  traceLog.scrollTop = traceLog.scrollHeight
}

function formatMessages(messages: unknown): string {
  if (!Array.isArray(messages)) return String(messages ?? '')
  return messages.map((m) => {
    const entry = record(m)
    const content = entry.content
    const contentText = typeof content === 'string' ? content : JSON.stringify(content)
    return `[${text(entry.role)}]\n${contentText}`
  }).join('\n\n')
}

function evictOldTurnCards(): void {
  while (turnOrder.length > MAX_TURN_CARDS) {
    const oldest = turnOrder.shift()
    if (oldest === undefined) break
    turnCards.get(oldest)?.cardEl.remove()
    turnCards.delete(oldest)
  }
}

function createTurnCard(turnId: number): TurnCard {
  if (turnCardsEl.querySelector('.empty')) turnCardsEl.replaceChildren()

  const cardEl = document.createElement('article')
  cardEl.className = 'trace-card'

  const header = document.createElement('header')
  header.className = 'trace-card-header'
  const title = document.createElement('span')
  title.className = 'trace-card-title'
  title.textContent = `Turn #${turnId}`
  const statusEl = document.createElement('span')
  statusEl.className = 'trace-card-status'
  statusEl.textContent = '进行中…'
  header.append(title, statusEl)

  const userEl = document.createElement('p')
  userEl.className = 'trace-card-user'

  const promptDetails = document.createElement('details')
  promptDetails.className = 'prompt-details'
  const summary = document.createElement('summary')
  summary.textContent = '发送的 Prompt'
  const promptBodyEl = document.createElement('pre')
  promptBodyEl.className = 'trace-detail'
  promptDetails.append(summary, promptBodyEl)

  const responseEl = document.createElement('p')
  responseEl.className = 'trace-card-response'

  const effectsEl = document.createElement('div')
  effectsEl.className = 'chip-row'

  cardEl.append(header, userEl, promptDetails, responseEl, effectsEl)
  turnCardsEl.prepend(cardEl)

  const card: TurnCard = {
    cardEl, userEl, statusEl, promptBodyEl, responseEl, effectsEl,
    chunkCount: 0, firstAt: null,
  }
  turnCards.set(turnId, card)
  turnOrder.push(turnId)
  evictOldTurnCards()
  return card
}

function getOrCreateTurnCard(turnId: number): TurnCard {
  return turnCards.get(turnId) ?? createTurnCard(turnId)
}

function applyTraceEntry(entry: Record<string, unknown>): void {
  const turnId = numeric(entry.turnId)
  if (turnId === null) {
    appendUngroupedEntry(entry)
    return
  }

  const card = getOrCreateTurnCard(turnId)
  if (card.firstAt === null) {
    const at = numeric(entry.at)
    if (at !== null) card.firstAt = at
  }

  switch (entry.kind) {
    case 'user_input':
      card.userEl.textContent = `你: ${text(entry.text)}`
      break
    case 'llm_request':
      card.promptBodyEl.textContent = formatMessages(entry.messages)
      break
    case 'llm_chunk':
      card.chunkCount++
      card.statusEl.textContent = `接收中…第 ${card.chunkCount} 段`
      break
    case 'llm_final': {
      card.responseEl.textContent = `月璃: ${text(entry.text)}`
      const at = numeric(entry.at)
      const elapsed = at !== null && card.firstAt !== null ? at - card.firstAt : null
      card.statusEl.textContent = elapsed !== null ? `完成 · ${elapsed} ms` : '完成'
      break
    }
    case 'memory_fact':
      chip(card.effectsEl, '记忆', `[${text(entry.memoryKind)}] ${text(entry.content)}`)
      break
    case 'mood_delta':
      chip(card.effectsEl, '心情', `favor=${fixed(entry.favor)} energy=${fixed(entry.energy)}`)
      break
    case 'llm_error':
      card.statusEl.textContent = `失败 · ${text(entry.errorKind)}`
      card.cardEl.classList.add('error')
      chip(card.effectsEl, '错误', text(entry.message))
      break
    default:
      break
  }
}

async function pollTrace(): Promise<void> {
  try {
    const entries = await window.observability?.readTrace(lastTraceSeq)
    if (!Array.isArray(entries) || !entries.length) return
    for (const entry of entries) {
      if (entry && typeof entry === 'object') applyTraceEntry(entry as Record<string, unknown>)
    }
    const last = entries[entries.length - 1] as Record<string, unknown>
    const seq = numeric(last.seq)
    if (seq !== null) lastTraceSeq = seq
    traceTotal += entries.length
    traceCount.textContent = `${traceTotal} 条 · 实时轮询中`
  } catch {
    /* 轮询失败静默重试，不打断快照那边的展示 */
  }
}

async function fetchAndRender(): Promise<void> {
  refreshButton.disabled = true
  try {
    const payload = await window.observability?.read()
    if (!payload) throw new Error('preload 未提供观察面板的只读接口')
    render(payload)
    fetchCount++
    document.body.dataset.fetchCount = String(fetchCount)
    const fetched = new Date()
    fetchedAt.dateTime = fetched.toISOString()
    fetchedAt.textContent = `读取于 ${new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(fetched)}`
  } catch (err) {
    grid.replaceChildren()
    const error = document.createElement('p')
    error.className = 'error'
    error.textContent = `读不到内部状态：${err instanceof Error ? err.message : String(err)}`
    grid.append(error)
  } finally {
    refreshButton.disabled = false
  }
}

openSettingsButton.addEventListener('click', () => window.observability?.openSettings())
refreshButton.addEventListener('click', () => void fetchAndRender())
autoRefresh.addEventListener('change', () => {
  if (autoRefreshTimer) clearInterval(autoRefreshTimer)
  autoRefreshTimer = autoRefresh.checked ? setInterval(() => void fetchAndRender(), AUTO_REFRESH_MS) : null
})

void fetchAndRender()
void pollTrace()
setInterval(() => void pollTrace(), TRACE_POLL_MS)
