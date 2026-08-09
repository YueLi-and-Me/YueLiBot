import type {
  ApiProviderConfig, ClientType, ModelDefinitionConfig, SelectionStrategy, YueliConfig,
} from '../shared/ipc.ts'

/**
 * 设置窗口：首次启动引导 + 后续编辑共用一套表单。
 *
 * URL 的 `?mode=first-run` 区分两种模式：首次启动时标题/文案不同，
 * 保存成功后不显示"重启"按钮（主进程本来就还没启动后端，不存在"重启"这回事），
 * 而是显示"正在启动…"——主进程收到保存成功的信号后会自己关掉这扇窗、继续正常流程。
 *
 * 表单分两半：
 *   · 静态字段（bot.* / tts.voice / advanced.* 等）走 name 路径绑定，和以前一样；
 *   · 服务商、模型、任务候选是可增删的列表，直接读写 loadedConfig，
 *     渲染出来的控件一律不带 name，免得被路径绑定当成静态字段处理。
 */

const params = new URLSearchParams(location.search)
const isFirstRun = params.get('mode') === 'first-run'

const form = document.getElementById('settings-form') as HTMLFormElement
const pageTitle = document.getElementById('page-title') as HTMLElement
const pageSubtitle = document.getElementById('page-subtitle') as HTMLElement
const statusText = document.getElementById('status-text') as HTMLElement
const saveButton = document.getElementById('save-button') as HTMLButtonElement
const restartButton = document.getElementById('restart-button') as HTMLButtonElement
const providerList = document.getElementById('provider-list') as HTMLElement
const modelList = document.getElementById('model-list') as HTMLElement
const taskList = document.getElementById('task-list') as HTMLElement
const addProviderButton = document.getElementById('add-provider') as HTMLButtonElement
const addModelButton = document.getElementById('add-model') as HTMLButtonElement

if (isFirstRun) {
  pageTitle.textContent = '欢迎使用月璃'
  pageSubtitle.textContent = '第一次启动需要先填一些基本信息，之后随时能从托盘菜单里的"设置"回来改。'
}

let loadedConfig: YueliConfig | null = null

/** 厂商预设。base_url 留空时由 Python 侧按 kind 选官方地址。 */
const PROVIDER_KINDS: Array<[string, string]> = [
  ['ark', '方舟（火山引擎）'],
  ['deepseek', 'DeepSeek'],
  ['dashscope', '通义千问'],
  ['moonshot', '月之暗面'],
  ['openai', 'OpenAI'],
  ['ollama', 'Ollama（本地）'],
  ['volcengine', '豆包语音'],
]

const TASK_LABELS: Array<[keyof YueliConfig['model_tasks'], string, string]> = [
  ['chat', '对话', '她说话用的模型。至少要有一个。'],
  ['vision', '看屏幕', '必须是能接受图片输入的多模态模型。'],
  ['tts', '语音合成', '协议由所属服务商决定：OpenAI 兼容或豆包语音。'],
  ['embedding', '向量记忆', '备用模型必须和主力输出同样的向量维度。'],
]

// ── DOM 小工具 ────────────────────────────────────────────────────────

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K, className = '', ...children: Array<Node | string>
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  if (className) node.className = className
  for (const child of children) node.append(child)
  return node
}

function textField(
  label: string, value: string, onInput: (value: string) => void,
  { password = false, placeholder = '' } = {},
): HTMLElement {
  const input = el('input')
  input.type = password ? 'password' : 'text'
  input.value = value
  input.placeholder = placeholder
  input.addEventListener('input', () => onInput(input.value))
  return el('label', 'field', el('span', 'field-label', label), input)
}

function numberField(label: string, value: number, onInput: (value: number) => void): HTMLElement {
  const input = el('input')
  input.type = 'number'
  input.value = String(value)
  input.addEventListener('input', () => {
    const parsed = Number(input.value)
    if (Number.isFinite(parsed)) onInput(parsed)
  })
  return el('label', 'field', el('span', 'field-label', label), input)
}

function selectField(
  label: string, value: string, options: Array<[string, string]>,
  onChange: (value: string) => void,
): HTMLElement {
  const select = el('select')
  for (const [optionValue, optionLabel] of options) {
    const option = el('option')
    option.value = optionValue
    option.textContent = optionLabel
    select.append(option)
  }
  select.value = value
  select.addEventListener('change', () => onChange(select.value))
  return el('label', 'field', el('span', 'field-label', label), select)
}

function iconButton(label: string, title: string, onClick: () => void): HTMLButtonElement {
  const button = el('button', 'button secondary icon')
  button.type = 'button'
  button.textContent = label
  button.title = title
  button.addEventListener('click', onClick)
  return button
}

// ── 服务商 ────────────────────────────────────────────────────────────

/**
 * 改名要把引用一起改掉。否则保存时才报「引用了不存在的厂商」，
 * 而用户刚做的其实是一次完全合理的重命名。
 */
function renameProvider(cfg: YueliConfig, before: string, after: string): void {
  for (const model of cfg.models) {
    if (model.api_provider === before) model.api_provider = after
  }
}

function renderProvider(cfg: YueliConfig, provider: ApiProviderConfig, index: number): HTMLElement {
  const card = el('div', 'repeat-item')
  const header = el('div', 'repeat-header',
    el('span', 'repeat-title', provider.name || '（未命名）'))
  header.append(iconButton('✕', '删除这个服务商', () => {
    cfg.api_providers.splice(index, 1)
    renderDynamicSections()
  }))
  card.append(header)

  card.append(textField('名称（模型挂在它下面）', provider.name, (value) => {
    renameProvider(cfg, provider.name, value)
    provider.name = value
    refreshProviderTitles()
  }))
  card.append(selectField('服务商预设', provider.kind, PROVIDER_KINDS, (value) => {
    provider.kind = value
  }))
  card.append(textField('Base URL', provider.base_url, (value) => { provider.base_url = value }, {
    placeholder: '留空用预设官方地址',
  }))
  card.append(textField('API Key', provider.api_key, (value) => { provider.api_key = value }, {
    password: true,
  }))

  const appIdField = textField('App ID（豆包语音的服务接口认证信息）', provider.app_id, (value) => {
    provider.app_id = value
  })
  card.append(selectField('请求协议', provider.client_type, [
    ['openai', 'OpenAI 兼容'],
    ['volcengine', '豆包语音（只能用于语音合成）'],
  ], (value) => {
    provider.client_type = value as ClientType
    appIdField.classList.toggle('collapsed', value !== 'volcengine')
  }))
  appIdField.classList.toggle('collapsed', provider.client_type !== 'volcengine')
  card.append(appIdField)

  const advanced = el('details', 'advanced-fields')
  advanced.append(el('summary', '', '高级'))
  advanced.append(numberField('超时（毫秒）', provider.timeout_ms, (value) => {
    provider.timeout_ms = value
  }))
  advanced.append(numberField('重试次数（用尽后才换下一家）', provider.max_retries, (value) => {
    provider.max_retries = value
  }))
  advanced.append(numberField('重试间隔（毫秒）', provider.retry_interval_ms, (value) => {
    provider.retry_interval_ms = value
  }))
  card.append(advanced)
  return card
}

/** 只更新卡片标题，不整块重绘——重绘会把用户正在打字的输入框焦点弄丢。 */
function refreshProviderTitles(): void {
  if (!loadedConfig) return
  const titles = providerList.querySelectorAll<HTMLElement>('.repeat-title')
  loadedConfig.api_providers.forEach((provider, index) => {
    const title = titles[index]
    if (title) title.textContent = provider.name || '（未命名）'
  })
}

// ── 模型 ──────────────────────────────────────────────────────────────

function renameModel(cfg: YueliConfig, before: string, after: string): void {
  for (const routing of Object.values(cfg.model_tasks)) {
    routing.model_list = routing.model_list.map((name) => (name === before ? after : name))
  }
}

function renderModel(cfg: YueliConfig, model: ModelDefinitionConfig, index: number): HTMLElement {
  const card = el('div', 'repeat-item')
  const header = el('div', 'repeat-header', el('span', 'repeat-title', model.name || '（未命名）'))
  header.append(iconButton('✕', '删除这个模型', () => {
    cfg.models.splice(index, 1)
    // 候选里也要跟着去掉，否则保存时会报引用不存在
    for (const routing of Object.values(cfg.model_tasks)) {
      routing.model_list = routing.model_list.filter((name) => name !== model.name)
    }
    renderDynamicSections()
  }))
  card.append(header)

  card.append(textField('名称（任务候选里引用它）', model.name, (value) => {
    renameModel(cfg, model.name, value)
    model.name = value
    refreshModelTitles()
  }))
  card.append(textField('模型 ID（发给接口的真实名字）', model.model_identifier, (value) => {
    model.model_identifier = value
  }, { placeholder: '例如 doubao-seed-character-260628' }))
  card.append(selectField(
    '所属服务商', model.api_provider,
    cfg.api_providers.map((provider) => [provider.name, provider.name || '（未命名）']),
    (value) => { model.api_provider = value },
  ))

  const advanced = el('details', 'advanced-fields')
  advanced.append(el('summary', '', '高级'))
  advanced.append(selectField('深度思考（仅方舟）', model.thinking, [
    ['disabled', '关闭（推荐，速度快）'],
    ['enabled', '开启'],
    ['auto', '自动'],
  ], (value) => { model.thinking = value as ModelDefinitionConfig['thinking'] }))
  advanced.append(el('p', 'hint', '⚠ 开启深度思考后实测首字延迟从 3 秒涨到 26~31 秒，桌宠场景不推荐。'))
  advanced.append(numberField('向量维度（只有 embedding 模型要填）', model.embedding_dim, (value) => {
    model.embedding_dim = value
  }))
  card.append(advanced)
  return card
}

function refreshModelTitles(): void {
  if (!loadedConfig) return
  const titles = modelList.querySelectorAll<HTMLElement>('.repeat-title')
  loadedConfig.models.forEach((model, index) => {
    const title = titles[index]
    if (title) title.textContent = model.name || '（未命名）'
  })
}

// ── 任务候选 ──────────────────────────────────────────────────────────

function renderTask(
  cfg: YueliConfig, task: keyof YueliConfig['model_tasks'], label: string, hint: string,
): HTMLElement {
  const routing = cfg.model_tasks[task]
  const card = el('div', 'repeat-item')
  card.append(el('div', 'repeat-header', el('span', 'repeat-title', label)))
  card.append(el('p', 'hint', hint))

  const modelOptions: Array<[string, string]> = cfg.models.map(
    (model) => [model.name, model.name || '（未命名）'],
  )

  routing.model_list.forEach((name, position) => {
    const row = el('div', 'candidate-row')
    const select = el('select')
    for (const [value, optionLabel] of modelOptions) {
      const option = el('option')
      option.value = value
      option.textContent = optionLabel
      select.append(option)
    }
    select.value = name
    select.addEventListener('change', () => {
      routing.model_list[position] = select.value
    })
    row.append(el('span', 'candidate-rank', position === 0 ? '主力' : `备${position}`))
    row.append(select)
    row.append(iconButton('↑', '往前排一位', () => {
      if (position === 0) return
      const list = routing.model_list
      ;[list[position - 1], list[position]] = [list[position]!, list[position - 1]!]
      renderDynamicSections()
    }))
    row.append(iconButton('↓', '往后排一位', () => {
      const list = routing.model_list
      if (position >= list.length - 1) return
      ;[list[position], list[position + 1]] = [list[position + 1]!, list[position]!]
      renderDynamicSections()
    }))
    row.append(iconButton('✕', '移出候选', () => {
      routing.model_list.splice(position, 1)
      renderDynamicSections()
    }))
    card.append(row)
  })

  if (routing.model_list.length === 0) {
    card.append(el('p', 'hint', '没有候选，这个任务不会工作。'))
  }

  const addButton = el('button', 'button secondary small')
  addButton.type = 'button'
  addButton.textContent = '添加候选'
  // 已经在候选里的模型不重复给——同一个模型排两遍只会让轮询白撞一次
  const available = cfg.models.filter((model) => !routing.model_list.includes(model.name))
  addButton.disabled = available.length === 0
  addButton.addEventListener('click', () => {
    const next = available[0]
    if (!next) return
    routing.model_list.push(next.name)
    renderDynamicSections()
  })
  card.append(addButton)

  if (routing.model_list.length > 1) {
    card.append(selectField('挑选顺序', routing.selection_strategy, [
      ['sequential', '按顺序（主力优先，挂了才顶上）'],
      ['random', '随机（把流量摊到多家）'],
    ], (value) => { routing.selection_strategy = value as SelectionStrategy }))
  }
  return card
}

// ── 渲染与收集 ────────────────────────────────────────────────────────

function renderDynamicSections(): void {
  const cfg = loadedConfig
  if (!cfg) return
  providerList.replaceChildren(
    ...cfg.api_providers.map((provider, index) => renderProvider(cfg, provider, index)),
  )
  modelList.replaceChildren(...cfg.models.map((model, index) => renderModel(cfg, model, index)))
  taskList.replaceChildren(
    ...TASK_LABELS.map(([task, label, hint]) => renderTask(cfg, task, label, hint)),
  )
  addModelButton.disabled = cfg.api_providers.length === 0
}

addProviderButton.addEventListener('click', () => {
  if (!loadedConfig) return
  loadedConfig.api_providers.push({
    name: uniqueName('服务商', loadedConfig.api_providers.map((p) => p.name)),
    kind: 'openai', base_url: '', api_key: '', client_type: 'openai', app_id: '',
    timeout_ms: 120_000, max_retries: 2, retry_interval_ms: 800,
  })
  renderDynamicSections()
})

addModelButton.addEventListener('click', () => {
  if (!loadedConfig) return
  const provider = loadedConfig.api_providers[0]
  if (!provider) return
  loadedConfig.models.push({
    name: uniqueName('模型', loadedConfig.models.map((m) => m.name)),
    model_identifier: '', api_provider: provider.name,
    thinking: 'disabled', embedding_dim: 0,
  })
  renderDynamicSections()
})

/** 新建时给个不撞车的名字——重名在保存时会被拒，不如一开始就避开。 */
function uniqueName(prefix: string, existing: string[]): string {
  for (let index = 1; ; index++) {
    const candidate = `${prefix}${index}`
    if (!existing.includes(candidate)) return candidate
  }
}

function getByPath(obj: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, key) => {
    if (acc && typeof acc === 'object') return (acc as Record<string, unknown>)[key]
    return undefined
  }, obj)
}

function setByPath(obj: Record<string, unknown>, path: string, value: unknown): void {
  const keys = path.split('.')
  let cursor = obj
  for (let i = 0; i < keys.length - 1; i++) {
    const key = keys[i]!
    if (typeof cursor[key] !== 'object' || cursor[key] === null) cursor[key] = {}
    cursor = cursor[key] as Record<string, unknown>
  }
  cursor[keys[keys.length - 1]!] = value
}

function populateForm(config: YueliConfig): void {
  for (const el of Array.from(form.elements)) {
    const name = (el as HTMLInputElement).name
    if (!name) continue
    const value = getByPath(config, name)
    if (value === undefined) continue
    if (el instanceof HTMLInputElement && el.type === 'checkbox') {
      el.checked = Boolean(value)
    } else if (el instanceof HTMLInputElement && el.dataset.stringArray === 'true') {
      if (!Array.isArray(value)) throw new Error(`${name} 必须是字符串数组`)
      el.value = value.join('，')
    } else if (el instanceof HTMLInputElement || el instanceof HTMLSelectElement) {
      el.value = String(value)
    }
  }
  syncToggleVisibility()
}

/** tts.enabled / vision.enabled 两个开关控制各自区块是否可见。 */
function syncToggleVisibility(): void {
  for (const body of Array.from(document.querySelectorAll<HTMLElement>('[data-toggle-target]'))) {
    const targetName = body.dataset.toggleTarget!
    const toggle = form.elements.namedItem(targetName) as HTMLInputElement | null
    body.classList.toggle('collapsed', !toggle?.checked)
  }
}

form.addEventListener('change', (e) => {
  if ((e.target as HTMLElement)?.matches?.('input[type="checkbox"][name$=".enabled"]')) syncToggleVisibility()
})

function collectFormValues(base: YueliConfig): YueliConfig {
  // 表单里没出现的字段（比如 conversation.*）保留原来读到的值，
  // 不能被表单提交时悄悄清空。服务商/模型/候选已经直接写在 base 上了。
  const result = JSON.parse(JSON.stringify(base)) as Record<string, unknown>
  for (const el of Array.from(form.elements)) {
    const input = el as HTMLInputElement | HTMLSelectElement
    if (!input.name) continue
    if (input instanceof HTMLInputElement && input.type === 'checkbox') {
      setByPath(result, input.name, input.checked)
    } else if (input instanceof HTMLInputElement && input.dataset.stringArray === 'true') {
      const values = input.value.split(/[,，、\n]/).map((value) => value.trim()).filter(Boolean)
      setByPath(result, input.name, values)
    } else if (input instanceof HTMLInputElement && input.type === 'number') {
      setByPath(result, input.name, Number(input.value))
    } else {
      setByPath(result, input.name, input.value)
    }
  }
  return result as unknown as YueliConfig
}

function setStatus(text: string, kind: 'ok' | 'error' | '' = ''): void {
  statusText.textContent = text
  statusText.className = `status-text ${kind}`
}

form.addEventListener('submit', async (e) => {
  e.preventDefault()
  if (!loadedConfig) return
  // 结构自检在主进程那一侧统一做（writeConfigDirectory），这里只负责把它
  // 返回的话显示出来——两边各留一套规则，迟早会对不上。
  const config = collectFormValues(loadedConfig)
  saveButton.disabled = true
  restartButton.hidden = true
  setStatus(isFirstRun ? '正在保存…' : '保存中…')
  try {
    const result = await window.settings?.save(config)
    if (!result?.ok) throw new Error(result?.error ?? '未知错误')
    loadedConfig = config
    renderDynamicSections()
    if (isFirstRun) {
      setStatus('保存成功，正在启动月璃…', 'ok')
      // 主进程监听同一次 save 调用的成功结果，会自己关掉这扇窗并继续启动——
      // 这里不用再做什么，保留提示文字直到窗口被关掉。
    } else {
      setStatus('已保存', 'ok')
      restartButton.hidden = false
    }
  } catch (err) {
    setStatus(`保存失败：${err instanceof Error ? err.message : String(err)}`, 'error')
  } finally {
    saveButton.disabled = false
  }
})

restartButton.addEventListener('click', () => {
  window.settings?.restartBackend()
  setStatus('已发送重启指令…', 'ok')
})

async function init(): Promise<void> {
  try {
    loadedConfig = (await window.settings?.read()) ?? null
    if (loadedConfig) {
      populateForm(loadedConfig)
      renderDynamicSections()
    }
  } catch (err) {
    setStatus(`读取配置失败：${err instanceof Error ? err.message : String(err)}`, 'error')
  }
}

void init()
