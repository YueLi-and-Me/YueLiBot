/**
 * 实现首次启动引导和后续配置编辑共用的表单控制器。
 *
 * 本模块负责 DOM 字段映射、表单校验、配置快照组装和保存反馈；持久化与后端重启
 * 通过 preload/settings.ts 执行，配置类型来自 electron/shared/ipc.ts。
 */
import type {
  ApiProviderConfig, AuthType, ClientType, ModelDefinitionConfig, ReasoningParseMode,
  SelectionStrategy, YueliConfig,
} from '../shared/ipc.ts'

/**
 * 设置窗口的表单控制器，兼容首次启动引导和后续配置编辑。
 *
 * `?mode=first-run` 只改变页面文案和保存后的窗口行为。静态配置字段通过 `name` 路径
 * 映射到配置对象；服务商、模型和任务候选使用可增删的动态节点，直接更新 `loadedConfig`，
 * 再由主进程的保存接口执行统一结构校验和持久化。
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
  pageTitle.textContent = '欢迎配置你的 Bot'
  pageSubtitle.textContent = '第一次启动需要先填一些基本信息，之后随时能从托盘菜单里的"设置"回来改。'
}

let loadedConfig: YueliConfig | null = null

/** 服务商预设；`base_url` 为空时由后端按 `kind` 选择内置地址。 */
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
  ['chat', '对话', 'Bot 说话用的模型。至少要有一个。'],
  ['proactive', '主动搭话', '留空时继承对话候选，也可以单独指定更快的模型。'],
  ['summary', '长期记忆摘要', '留空时继承对话候选，也可以单独指定低成本模型。'],
  ['schedule', '每日生活计划', '留空时继承对话候选，也可以单独指定结构化输出模型。'],
  ['vision', '看屏幕', '必须是能接受图片输入的多模态模型。'],
  ['tts', '语音合成', '协议由所属服务商决定：OpenAI 兼容或豆包语音。'],
  ['embedding', '向量记忆', '同一任务下的候选模型必须输出同样的向量维度。'],
]

// DOM 构造工具。

/**
 * 创建指定标签、类名和子节点组成的 DOM 元素。
 *
 * @param tag HTML 标签名。
 * @param className CSS 类名；默认值为空字符串。
 * @param children 要追加的节点或文本子项。
 * @returns 与标签名对应的 HTML 元素。
 * @throws 传播浏览器 DOM 创建或追加失败产生的异常。
 */
function el<K extends keyof HTMLElementTagNameMap>(
  tag: K, className = '', ...children: Array<Node | string>
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  if (className) node.className = className
  for (const child of children) node.append(child)
  return node
}

/**
 * 创建文本或密码输入字段，并在输入事件发生时回调最新文本。
 *
 * @param label 字段展示标签。
 * @param value 初始文本值。
 * @param onInput 输入回调，参数为当前输入框文本。
 * @param options 可选显示配置；`password` 默认值为 `false`，`placeholder` 默认为空。
 * @returns 包含标签和输入框的字段容器。
 */
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

/**
 * 创建数字输入字段，并仅在输入可转换为有限数字时触发回调。
 *
 * @param label 字段展示标签。
 * @param value 初始数字值。
 * @param onInput 数字输入回调。
 * @returns 包含标签和数字输入框的字段容器。
 */
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

/**
 * 创建单选下拉字段，并在选项变化时回调选中值。
 *
 * @param label 字段展示标签。
 * @param value 初始选中值。
 * @param options 二元组数组，元素依次为选项值和展示文本。
 * @param onChange 选项变化回调。
 * @returns 包含标签和下拉框的字段容器。
 */
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

/**
 * 创建动态配置使用的复选开关。
 *
 * @param label 开关右侧的中文说明。
 * @param checked 初始开关状态。
 * @param onChange 状态变化回调。
 * @returns 已绑定变化事件的标签与复选框组合。
 */
function checkboxField(
  label: string,
  checked: boolean,
  onChange: (checked: boolean) => void,
): HTMLElement {
  const input = el('input')
  input.type = 'checkbox'
  input.checked = checked
  input.addEventListener('change', () => onChange(input.checked))
  return el('label', 'field checkbox-field', input, el('span', '', label))
}

/**
 * 创建不提交表单的图标按钮。
 *
 * @param label 按钮显示文本或符号。
 * @param title 按钮无障碍提示和悬停标题。
 * @param onClick 点击回调。
 * @returns 已绑定点击事件的按钮元素。
 */
function iconButton(label: string, title: string, onClick: () => void): HTMLButtonElement {
  const button = el('button', 'button secondary icon')
  button.type = 'button'
  button.textContent = label
  button.title = title
  button.addEventListener('click', onClick)
  return button
}

// 服务商动态配置。

/**
 * 修改服务商名称，并同步更新所有模型的服务商引用。
 *
 * @param cfg 当前可编辑配置。
 * @param before 原服务商名称。
 * @param after 新服务商名称。
 * @returns 无返回值；配置对象会被原地修改。
 */
function renameProvider(cfg: YueliConfig, before: string, after: string): void {
  for (const model of cfg.models) {
    if (model.api_provider === before) model.api_provider = after
  }
}

/**
 * 创建单个服务商的动态编辑卡片，并绑定删除、字段更新和鉴权模式切换事件。
 *
 * @param cfg 当前可编辑配置；删除服务商时同步触发整组动态区域重绘。
 * @param provider 当前服务商配置对象。
 * @param index 服务商在配置数组中的索引，用于删除操作。
 * @returns 服务商编辑卡片的根元素。
 * @throws 传播 DOM 创建、事件绑定或重绘过程中产生的异常。
 */
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

  // 鉴权名称只在 header/query 模式可见，协议切换时同时更新三处字段的可见性。
  const authNameField = textField('鉴权字段名', provider.auth_name, (value) => {
    provider.auth_name = value
  }, { placeholder: 'header 或 query 模式必填' })
  const authTypeField = selectField('鉴权方式', provider.auth_type, [
    ['bearer', 'Bearer'],
    ['header', '自定义请求头'],
    ['query', 'Query 参数'],
    ['none', '无鉴权'],
  ], (value) => {
    provider.auth_type = value as AuthType
    authNameField.classList.toggle('collapsed', value !== 'header' && value !== 'query')
  })
  authNameField.classList.toggle(
    'collapsed', provider.auth_type !== 'header' && provider.auth_type !== 'query',
  )
  card.append(authTypeField, authNameField)

  const appIdField = textField('App ID（豆包语音的服务接口认证信息）', provider.app_id, (value) => {
    provider.app_id = value
  })
  card.append(selectField('请求协议', provider.client_type, [
    ['openai', 'OpenAI 兼容'],
    ['volcengine', '豆包语音（只能用于语音合成）'],
  ], (value) => {
    provider.client_type = value as ClientType
    appIdField.classList.toggle('collapsed', value !== 'volcengine')
    authTypeField.classList.toggle('collapsed', value !== 'openai')
    authNameField.classList.toggle(
      'collapsed',
      value !== 'openai' || (provider.auth_type !== 'header' && provider.auth_type !== 'query'),
    )
  }))
  appIdField.classList.toggle('collapsed', provider.client_type !== 'volcengine')
  authTypeField.classList.toggle('collapsed', provider.client_type !== 'openai')
  authNameField.classList.toggle(
    'collapsed',
    provider.client_type !== 'openai'
      || (provider.auth_type !== 'header' && provider.auth_type !== 'query'),
  )
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

/**
 * 更新服务商卡片标题而不重绘整个动态区域。
 *
 * @returns 无返回值；配置未加载或对应标题节点不存在时跳过。
 * @remarks 局部更新用于保留用户正在编辑的输入框焦点和光标位置。
 */
function refreshProviderTitles(): void {
  if (!loadedConfig) return
  const titles = providerList.querySelectorAll<HTMLElement>('.repeat-title')
  loadedConfig.api_providers.forEach((provider, index) => {
    const title = titles[index]
    if (title) title.textContent = provider.name || '（未命名）'
  })
}

// 模型动态配置。

/**
 * 修改模型名称，并同步更新所有任务路由中的候选引用。
 *
 * @param cfg 当前可编辑配置。
 * @param before 原模型名称。
 * @param after 新模型名称。
 * @returns 无返回值；配置对象会被原地修改。
 */
function renameModel(cfg: YueliConfig, before: string, after: string): void {
  for (const routing of Object.values(cfg.model_tasks)) {
    routing.model_list = routing.model_list.map((name) => (name === before ? after : name))
  }
}

/**
 * 创建单个模型的动态编辑卡片，并绑定删除、重命名和字段更新事件。
 *
 * @param cfg 当前可编辑配置；删除模型时同步移除全部任务候选引用。
 * @param model 当前模型配置对象。
 * @param index 模型在配置数组中的索引，用于删除操作。
 * @returns 模型编辑卡片的根元素。
 * @throws 传播 DOM 创建、事件绑定或重绘过程中产生的异常。
 */
function renderModel(cfg: YueliConfig, model: ModelDefinitionConfig, index: number): HTMLElement {
  const card = el('div', 'repeat-item')
  const header = el('div', 'repeat-header', el('span', 'repeat-title', model.name || '（未命名）'))
  header.append(iconButton('✕', '删除这个模型', () => {
    cfg.models.splice(index, 1)
    // 删除模型时同步移除任务候选引用，保证保存前不会留下悬空模型名称。
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
  card.append(checkboxField(
    '启用思考（未显式配置时沿用模型默认行为）',
    model.extra_body.enable_thinking !== false,
    (checked) => {
      model.extra_body = { ...model.extra_body, enable_thinking: checked }
    },
  ))

  const advanced = el('details', 'advanced-fields')
  advanced.append(el('summary', '', '高级'))
  advanced.append(selectField('推理内容解析', model.reasoning_parse_mode, [
    ['field', '接口字段'],
    ['tag', '<think> 标签'],
    ['none', '不解析'],
  ], (value) => { model.reasoning_parse_mode = value as ReasoningParseMode }))
  advanced.append(numberField('向量维度（只有 embedding 模型要填）', model.embedding_dim, (value) => {
    model.embedding_dim = value
  }))
  card.append(advanced)
  return card
}

/**
 * 更新模型卡片标题而不重绘整个动态区域。
 *
 * @returns 无返回值；配置未加载或对应标题节点不存在时跳过。
 */
function refreshModelTitles(): void {
  if (!loadedConfig) return
  const titles = modelList.querySelectorAll<HTMLElement>('.repeat-title')
  loadedConfig.models.forEach((model, index) => {
    const title = titles[index]
    if (title) title.textContent = model.name || '（未命名）'
  })
}

// 任务候选动态配置。

/**
 * 创建单个任务的候选模型和超时参数编辑卡片。
 *
 * @param cfg 当前可编辑配置。
 * @param task 任务路由键。
 * @param label 任务展示名称。
 * @param hint 任务用途说明。
 * @returns 任务编辑卡片的根元素。
 * @throws 传播 DOM 创建、事件绑定或重绘过程中产生的异常。
 * @remarks 候选列表去重，主候选通过位置 `0` 表示；候选顺序变化后立即重绘以更新序号。
 */
function renderTask(
  cfg: YueliConfig, task: keyof YueliConfig['model_tasks'], label: string, hint: string,
): HTMLElement {
  const routing = cfg.model_tasks[task]
  const card = el('div', 'repeat-item')
  card.append(el('div', 'repeat-header', el('span', 'repeat-title', label)))
  card.append(el('p', 'hint', hint))
  card.append(numberField('首字超时（毫秒）', routing.first_token_timeout_ms, (value) => {
    routing.first_token_timeout_ms = value
  }))
  card.append(numberField('慢请求阈值（毫秒，0 关闭）', routing.slow_threshold_ms, (value) => {
    routing.slow_threshold_ms = value
  }))

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
    row.append(el('span', 'candidate-rank', `#${position + 1}`))
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
  // 已在候选中的模型不再提供添加选项，避免重复候选改变挑选顺序和故障切换行为。
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
      ['sequential', '按顺序（从上往下试，挂了才顶下一个）'],
      ['random', '随机（每次打乱候选）'],
      ['balance', '负载均衡（健康模型逐轮分摊）'],
    ], (value) => { routing.selection_strategy = value as SelectionStrategy }))
  }
  return card
}

// 动态区域渲染与表单收集。

/**
 * 按当前配置重绘服务商、模型和任务候选三个动态区域。
 *
 * @returns 无返回值；未加载配置时跳过。
 * @throws 传播动态卡片创建和 DOM 替换过程中的异常。
 */
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
    auth_type: 'bearer', auth_name: '',
    model_list_endpoint: '/models', default_headers: {}, default_query: {},
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
    extra_body: {}, reasoning_parse_mode: 'field',
    visual: false, temperature: null, max_tokens: null, price_in: 0, price_out: 0,
    embedding_dim: 0,
  })
  renderDynamicSections()
})

/**
 * 生成不与现有名称重复的顺序名称。
 *
 * @param prefix 名称前缀。
 * @param existing 已占用名称数组。
 * @returns 从 `${prefix}1` 开始递增且未被占用的名称。
 * @remarks 方法在找到空闲名称前持续递增；名称数量异常大时线性检查会增加开销。
 */
function uniqueName(prefix: string, existing: string[]): string {
  for (let index = 1; ; index++) {
    const candidate = `${prefix}${index}`
    if (!existing.includes(candidate)) return candidate
  }
}

/**
 * 按点分隔路径读取嵌套配置值。
 *
 * @param obj 配置对象或未知根值。
 * @param path 点分隔字段路径，例如 `bot.name`。
 * @returns 路径对应的值；中间节点不是对象或路径不存在时返回 `undefined`。
 */
function getByPath(obj: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, key) => {
    if (acc && typeof acc === 'object') return (acc as Record<string, unknown>)[key]
    return undefined
  }, obj)
}

/**
 * 按点分隔路径写入嵌套配置值，并为缺失的中间对象创建记录。
 *
 * @param obj 待修改的可变配置记录。
 * @param path 点分隔字段路径；必须至少包含一个字段名。
 * @param value 要写入的值。
 * @returns 无返回值；对象会被原地修改。
 * @throws Error 当路径为空导致无法确定目标字段时传播索引访问错误。
 */
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

/**
 * 使用配置对象填充静态表单字段，并同步功能开关控制的区块可见性。
 *
 * @param config 已加载的运行时配置。
 * @returns 无返回值。
 * @throws Error 当声明为字符串数组的字段实际不是数组，或 DOM 字段类型不一致时抛出。
 */
function populateForm(config: YueliConfig): void {
  for (const el of Array.from(form.elements)) {
    const name = (el as HTMLInputElement).name
    if (!name) continue
    const value = getByPath(config, name)
    if (value === undefined) continue
    if (el instanceof HTMLInputElement && el.type === 'checkbox') {
      el.checked = Boolean(value)
    } else if (
      (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)
      && el.dataset.stringArray === 'true'
    ) {
      if (!Array.isArray(value)) throw new Error(`${name} 必须是字符串数组`)
      el.value = el instanceof HTMLTextAreaElement ? value.join('\n') : value.join('，')
    } else if (
      el instanceof HTMLInputElement
      || el instanceof HTMLSelectElement
      || el instanceof HTMLTextAreaElement
    ) {
      el.value = String(value)
    }
  }
  syncToggleVisibility()
}

/**
 * 根据表单中的功能开关更新关联配置区块的折叠状态。
 *
 * @returns 无返回值；找不到对应开关时按未启用处理。
 */
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

/**
 * 将静态表单字段合并回配置快照，保留页面未展示的字段和动态配置数组。
 *
 * @param base 已加载的配置快照；方法不会修改该对象。
 * @returns 深拷贝后应用表单值的完整配置对象。
 * @throws Error 当表单控件声明为字符串数组但值无法按预期处理时抛出。
 * @remarks 数字字段通过 `Number` 转换，复选框保存为布尔值，字符串数组按控件类型分别按行或逗号拆分。
 */
function collectFormValues(base: YueliConfig): YueliConfig {
  // 先深拷贝基础配置，保留未出现在表单中的会话参数及动态列表，避免提交时静默清空。
  const result = JSON.parse(JSON.stringify(base)) as Record<string, unknown>
  for (const el of Array.from(form.elements)) {
    const input = el as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
    if (!input.name) continue
    if (input instanceof HTMLInputElement && input.type === 'checkbox') {
      setByPath(result, input.name, input.checked)
    } else if (
      (input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement)
      && input.dataset.stringArray === 'true'
    ) {
      const separator = input instanceof HTMLTextAreaElement ? /\r?\n/ : /[,，、\n]/
      const values = input.value.split(separator).map((value) => value.trim()).filter(Boolean)
      setByPath(result, input.name, values)
    } else if (input instanceof HTMLInputElement && input.type === 'number') {
      setByPath(result, input.name, Number(input.value))
    } else {
      setByPath(result, input.name, input.value)
    }
  }
  return result as unknown as YueliConfig
}

/**
 * 更新设置页状态文本及对应的样式状态。
 *
 * @param text 要展示的状态信息。
 * @param kind 状态类型：`ok`、`error` 或空字符串，默认值为空字符串。
 * @returns 无返回值。
 */
function setStatus(text: string, kind: 'ok' | 'error' | '' = ''): void {
  statusText.textContent = text
  statusText.className = `status-text ${kind}`
}

form.addEventListener('submit', async (e) => {
  e.preventDefault()
  if (!loadedConfig) return
  // 结构校验和持久化由主进程统一负责；渲染层只组装输入、提交并展示返回结果。
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
      setStatus(`保存成功，正在启动${config.bot.name}…`, 'ok')
      // 首次启动由主进程在同一次保存成功后关闭窗口并继续启动，此处只保留过渡提示。
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

/**
 * 读取配置、填充静态字段并首次渲染动态配置区域。
 *
 * @returns 页面初始化完成后的 Promise。
 * @throws 不向上抛出读取或渲染异常；错误转换为设置页状态提示。
 */
async function init(): Promise<void> {
  try {
    loadedConfig = (await window.settings?.read()) ?? null
    if (loadedConfig) {
      populateForm(loadedConfig)
      renderDynamicSections()
      const configuredName = loadedConfig.bot.name.trim()
      if (!isFirstRun && configuredName) {
        pageTitle.textContent = `${configuredName}设置`
        document.title = `${configuredName}设置`
      }
      restartButton.textContent = configuredName
        ? `重启${configuredName}使配置生效`
        : '重启 Bot 使配置生效'
    }
  } catch (err) {
    setStatus(`读取配置失败：${err instanceof Error ? err.message : String(err)}`, 'error')
  }
}

void init()
