/**
 * 读取、校验并持久化 Electron 设置窗口使用的运行配置。
 *
 * 本模块负责兼容旧配置文件、解析 TOML、创建配置目录并生成前端可安全展示的
 * 配置快照；它依赖 shared/ipc.ts 的配置类型，并由主进程和设置窗口共同调用。
 */
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { parse as parseDotenv } from 'dotenv'
import * as TOML from 'smol-toml'

import type {
  ApiProviderConfig, AuthType, ClientType, ModelDefinitionConfig, SelectionStrategy,
  TaskRoutingConfig, YueliConfig,
} from '../shared/ipc.ts'

/**
 * 持久化配置是分层的：API 厂商 → 具体模型 → 任务引用。
 * 设置页仍使用便于表单编辑的 YueliConfig，读写边界负责双向转换。
 */

/**
 * 配置格式版本。**删除或重命名配置字段时必须 bump 它**——`directoryIsStale()`
 * 只比版本号、不比字段；不 bump 就不会重写，废弃字段会一直留在用户文件里，
 * 后端每次启动都要为它们报一次「配置字段变更」。
 *
 * bump 之前必须先确认写入器是 schema 的超集（`scripts/check/config_parity.py` 守这一条）：
 * 模板缺字段时重写会静默清掉用户的配置，1.2.0 这次就是先补齐写入器才敢动版本号的。
 *
 * 1.1.0 起 model_tasks 从「一个任务一个模型名」改成「一个任务一串候选模型 +
 * 轮询策略」。读到 1.0.0 会按旧形态解析并在下次保存时升级，不会拒绝启动。
 * 1.2.0 移除日程时刻表遗留字段（min_slots / max_slots / fallback_* / bedtime_*），
 * 它们已被活动时间线取代；旧文件按 1.1.0 解析后重写即自动清除。
 * 1.3.0 新增 [emoji] 与 [emoji.cleanup] 表情包库管理段；旧文件按 1.2.0 解析后
 * 重写即补齐两段及默认值。
 * 1.4.0 新增 conversation.private_facts_in_group，控制私聊来源事实能否进群聊；
 * 旧文件按 1.3.0 解析后重写即补齐该字段及默认值。
 * 1.5.0 新增 [memory_feedback] 段：N4 反馈纠错链路，15 项默认全关；
 * 旧文件按 1.4.0 解析后重写即补齐该段及默认值。
 *
 * [developer] 段（开发者命令通道）有意不 bump 版本号：整段可选、缺失即关闭，
 * 且初始配置生成时会显式剔除它，用户文件里永远不出现，没有内容需要迁移。
 * 详细理由见 src/core/config/schema.py 的 InnerConfig 注释。
 */
export const CONFIG_VERSION = '1.5.0'
const SUPPORTED_VERSIONS = [
  '1.0.0', '1.1.0', '1.2.0', '1.3.0', '1.4.0', '1.5.0',
] as const
const CONFIG_FILES = ['providers.toml', 'models.toml', 'bot.toml', 'features.toml'] as const
export const MODEL_TASKS = [
  'chat', 'proactive', 'summary', 'schedule', 'vision', 'expression',
  'planner', 'replyer', 'scene', 'memory', 'tts', 'embedding',
] as const

/**
 * 生成停用状态的 QQ 适配器连接配置模板。
 *
 * 连接段名由适配器清单的 config_section 决定，不写死在模板里：段名一旦有两个
 * 独立来源，换适配器就会写出一份该适配器读不了的配置，且只在启动时表现为
 * 「缺少 [xxx] 配置段」，从模板本身看不出任何异常。
 *
 * @param section 适配器清单声明的连接段名。
 * @returns 完整的 TOML 文本，连接默认停用。
 */
function adapterConfigTemplate(section: string): string {
  return `# Bot 的 QQ 配置。self_qq 和 owner.qq 是两个号，别填反。

[inner]
version = "0.1.0"

[${section}]
enabled = false              # 改成 true 才连 QQ
self_qq = ""                 # Bot 的号：协议端登录的那个
host = "127.0.0.1"           # 协议端在本机就不用改
port = 8095                  # 协议端那条正向 WebSocket 的端口
token = ""                   # 那条连接的令牌，没设就留空
reconnect_interval_sec = 5   # 断线后几秒重连
action_timeout_sec = 15      # 请求几秒算超时

[owner]
qq = ""                      # 你的号：平时发消息用的那个

[private]
mode = "whitelist"           # whitelist 只回名单里的人；blacklist 只不回名单里的人
list = []                    # 数字 QQ 号，你自己不用写

[group]
mode = "whitelist"           # 群聊固定使用白名单
list = []                    # 数字 QQ 群号；用户本人在群里也不会豁免名单外群
`
}

/** 新装时的唯一一条连接。用户可以在设置页继续添加备用厂商。 */
const DEFAULT_PROVIDER: ApiProviderConfig = {
  name: '主力', kind: 'ark', base_url: '', api_key: '', client_type: 'openai',
  auth_type: 'bearer', auth_name: '',
  app_id: '', model_list_endpoint: '/models', default_headers: {}, default_query: {},
  timeout_ms: 120_000, max_retries: 2, retry_interval_ms: 800,
}

export const DEFAULT_CONFIG: YueliConfig = {
  bot: { name: '', aliases: [], user_nickname: '', relationship: '' },
  group_chat: {
    at_mention_must_reply: true,
    name_mention_probability: 1,
    presence_decay_strength: 3,
    persona_weight: 0.05,
    reply_window_minutes: 10,
    max_replies_in_window: 3,
    reactions_enabled: true,
    pokes_enabled: false,
    self_started_topics: true,
    scene_refresh_messages: 15,
  },
  schedule: {
    sleep_enabled: true,
    fallback_theme: '按自己的节奏度过今天。',
    generation_retry_interval_minutes: 10,
  },
  personality: {
    birthday: '',
    personality: '',
    reply_style: '',
    tone_probability: 0,
    tone_variants: [],
  },
  conversation: {
    working_memory_messages: 40,
    summarize_trigger_messages: 48,
    summarize_batch_messages: 16,
    session_gap_minutes: 30,
    fact_recall_limit: 6,
    recalled_episode_limit: 2,
    recent_episode_limit: 2,
    episode_context_limit: 3,
    fact_extract_trigger_messages: 32,
    fact_extract_batch_messages: 12,
    private_facts_in_group: false,
  },
  conversation_agent: {
    mode: 'off',
    selected_streams: [],
    trigger_mode: 'signal',
    frequency_talk_value: 0.6,
    reply_necessity_threshold: 80,
    max_cognitive_rounds: 2,
    split_replyer: true,
    tool_calling: true,
  },
  typing: {
    bubble_target_chars: 18,
    max_bubbles_per_say: 3,
    delay_enabled: true,
    chinese_char_seconds: 0.28,
    latin_char_seconds: 0.12,
    send_gap_seconds: 0.4,
    max_delay_seconds: 8,
    emoji_pick_seconds: 1.5,
    follow_up: { enabled: true, peer_silence_minutes: 1 },
    nudge: { enabled: true, peer_silence_minutes: 3, max_per_silence: 2 },
  },
  emoji: {
    max_count: 1000,
    auto_evict: true,
    check_interval_minutes: 5,
    max_file_size_mb: 5,
    content_filtration: false,
    collect_enabled: true,
    cleanup: {
      enabled: true,
      check_interval_hours: 6,
      orphan_retention_days: 30,
    },
  },
  desktop_pet: { enabled: false },
  generation: {
    chat: { temperature: 0.85, max_tokens: 0 },
    proactive: { enabled: true, temperature: 0.9, max_tokens: 200 },
    summary: { temperature: 0.3, max_tokens: 0 },
    schedule: { temperature: 0.95, max_tokens: 4096 },
    expression: { temperature: 0.1, max_tokens: 4096 },
    vision: { temperature: 0.3, max_tokens: 120 },
    planner: { temperature: 0.85, max_tokens: 0 },
    replyer: { temperature: 0.85, max_tokens: 0 },
    scene: { temperature: 0.3, max_tokens: 0 },
    memory: { temperature: 0.1, max_tokens: 1024 },
  },
  api_providers: [DEFAULT_PROVIDER],
  models: [{
    name: 'chat', model_identifier: '', api_provider: '主力',
    extra_body: {}, reasoning_parse_mode: 'field',
    visual: false, temperature: null, max_tokens: null, price_in: 0, price_out: 0,
    embedding_dim: 0,
  }],
  model_tasks: {
    chat: { model_list: ['chat'], selection_strategy: 'sequential', first_token_timeout_ms: 30_000, slow_threshold_ms: 8_000 },
    proactive: { model_list: [], selection_strategy: 'sequential', first_token_timeout_ms: 30_000, slow_threshold_ms: 8_000 },
    summary: { model_list: [], selection_strategy: 'sequential', first_token_timeout_ms: 30_000, slow_threshold_ms: 8_000 },
    schedule: { model_list: [], selection_strategy: 'sequential', first_token_timeout_ms: 30_000, slow_threshold_ms: 8_000 },
    vision: { model_list: [], selection_strategy: 'sequential', first_token_timeout_ms: 30_000, slow_threshold_ms: 8_000 },
    expression: { model_list: [], selection_strategy: 'sequential', first_token_timeout_ms: 30_000, slow_threshold_ms: 8_000 },
    planner: { model_list: [], selection_strategy: 'sequential', first_token_timeout_ms: 30_000, slow_threshold_ms: 8_000 },
    replyer: { model_list: [], selection_strategy: 'sequential', first_token_timeout_ms: 30_000, slow_threshold_ms: 8_000 },
    scene: { model_list: [], selection_strategy: 'sequential', first_token_timeout_ms: 30_000, slow_threshold_ms: 8_000 },
    memory: { model_list: [], selection_strategy: 'sequential', first_token_timeout_ms: 30_000, slow_threshold_ms: 8_000 },
    tts: { model_list: [], selection_strategy: 'sequential', first_token_timeout_ms: 30_000, slow_threshold_ms: 8_000 },
    embedding: { model_list: [], selection_strategy: 'sequential', first_token_timeout_ms: 30_000, slow_threshold_ms: 8_000 },
  },
  tts: {
    enabled: false, voice: '', format: 'mp3', speed: 0.95, cluster: 'volcano_tts',
  },
  vision: {
    enabled: false, chat_image_enabled: false, fullscreen_silent: true, capture_mode: 'window',
  },
  perception: {
    surfaces: ['desktop'],
  },
  vector: {
    enabled: false,
  },
  memory_feedback: {
    enabled: false,
    window_hours: 12,
    check_interval_minutes: 30,
    batch_size: 20,
    auto_apply_threshold: 0.85,
    max_feedback_messages: 30,
    prefilter_enabled: true,
    mark_enabled: true,
    hard_filter_enabled: true,
    profile_refresh_enabled: true,
    profile_force_refresh_on_read: true,
    episode_rebuild_enabled: true,
    episode_query_block_enabled: true,
    reconcile_interval_minutes: 5,
    reconcile_batch_size: 20,
  },
  log: {
    level: 'INFO', console_level: '', file_level: '',
    level_style: 'lite', color_scope: 'full', date_format: '%m-%d %H:%M:%S',
    to_file: true,
    file_max_bytes: 5 * 1024 * 1024, max_files: 30, cleanup_days: 14,
    library_levels: { httpx: 'WARNING', httpcore: 'WARNING', PIL: 'WARNING' },
    suppress_libraries: ['urllib3'],
    request_snapshots: true, max_snapshot_files: 50,
    prompt_records: true, max_prompt_records_per_task: 200,
    event_retention_count: 20_000, event_retention_hours: 72,
  },
  advanced: {
    https_proxy: '',
  },
  developer: {
    enabled: false,
  },
}

type ModelTask = (typeof MODEL_TASKS)[number]
type GenerationConfig = YueliConfig['generation']

/**
 * 创建默认配置的深拷贝，避免调用方修改全局默认值。
 *
 * @returns 与默认配置结构相同且可独立修改的配置对象。
 */
export function cloneDefaults(): YueliConfig {
  return structuredClone(DEFAULT_CONFIG)
}

/**
 * 判断未知值是否为非数组对象，并将其收窄为键值记录。
 *
 * @param value 待判断的未知配置值。
 * @returns 值为非空、非数组对象时返回 `true`，否则返回 `false`。
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * 从配置记录中读取必需的子表。
 *
 * @param record 当前配置记录。
 * @param key 子表字段名。
 * @param path 用于错误信息的配置文件路径或字段路径。
 * @returns 指定字段对应的配置记录。
 * @throws Error 当字段缺失、值为空或值不是对象表时抛出。
 */
function recordAt(record: Record<string, unknown>, key: string, path: string): Record<string, unknown> {
  const value = record[key]
  if (!isRecord(value)) throw new Error(`${path} 缺少 [${key}] 配置段`)
  return value
}

/**
 * 从配置记录中读取必需的字符串字段。
 *
 * @param record 当前配置记录。
 * @param key 字段名。
 * @param path 用于错误信息的配置文件路径或字段路径。
 * @returns 字段的字符串值；该方法不负责去除首尾空白。
 * @throws Error 当字段不存在或值不是字符串时抛出。
 */
function stringAt(record: Record<string, unknown>, key: string, path: string): string {
  const value = record[key]
  if (typeof value !== 'string') throw new Error(`${path} 的 ${key} 必须是字符串`)
  return value
}

/**
 * 从配置记录中读取必需的有限数字字段。
 *
 * @param record 当前配置记录。
 * @param key 字段名。
 * @param path 用于错误信息的配置文件路径或字段路径。
 * @returns 字段的有限数字值。
 * @throws Error 当字段不存在、值不是数字或值为 `NaN`/无穷大时抛出。
 */
function numberAt(record: Record<string, unknown>, key: string, path: string): number {
  const value = record[key]
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`${path} 的 ${key} 必须是数字`)
  return value
}

/**
 * 读取可选数字字段；字段缺失时返回调用方提供的默认值。
 *
 * @param record 当前配置记录。
 * @param key 字段名。
 * @param defaultValue 字段缺失时使用的有限数字默认值。
 * @param path 用于错误信息的配置文件路径或字段路径。
 * @returns 配置中的数字值，或字段缺失时的 `defaultValue`。
 * @throws Error 当字段存在但不是有限数字时抛出。
 */
function numberAtOr(
  record: Record<string, unknown>, key: string, defaultValue: number, path: string,
): number {
  if (record[key] === undefined) return defaultValue
  return numberAt(record, key, path)
}

/**
 * 读取可选字符串字段；字段缺失时返回调用方提供的默认值。
 *
 * @param record 当前配置记录。
 * @param key 字段名。
 * @param defaultValue 字段缺失时使用的字符串默认值。
 * @param path 用于错误信息的配置文件路径或字段路径。
 * @returns 配置中的字符串值，或字段缺失时的 `defaultValue`。
 * @throws Error 当字段存在但不是字符串时抛出。
 */
function stringAtOr(
  record: Record<string, unknown>, key: string, defaultValue: string, path: string,
): string {
  if (record[key] === undefined) return defaultValue
  return stringAt(record, key, path)
}

/**
 * 读取视觉截图范围，并在配置加载阶段校验取值。
 *
 * @param record 视觉配置记录。
 * @param path 用于错误信息的配置文件路径或字段路径。
 * @returns `window`（前台窗口）或 `screen`（整个主屏）；字段缺失时返回默认值。
 * @throws Error 当字段取值不是 `window` 或 `screen` 时抛出。
 */
function captureModeAt(record: Record<string, unknown>, path: string): 'window' | 'screen' {
  const value = record['capture_mode']
  if (value === undefined) return DEFAULT_CONFIG.vision.capture_mode
  if (value !== 'window' && value !== 'screen') {
    throw new Error(`${path} 的 capture_mode 只能是 "window" 或 "screen"`)
  }
  return value
}

/**
 * 读取并校验允许生成屏幕情境的出口列表。
 *
 * @param document 已解析的配置文档。
 * @param path 用于错误信息的配置文件路径或字段路径。
 * @returns 仅包含 `desktop` 或 `direct` 的新数组；配置段缺失时返回默认副本。
 * @throws Error 当字段不是字符串数组、包含群聊出口或包含未知出口时抛出。
 */
function perceptionSurfacesAt(
  document: Record<string, unknown>, path: string,
): Array<'desktop' | 'direct'> {
  if (document.perception === undefined) return [...DEFAULT_CONFIG.perception.surfaces]
  const perception = recordAt(document, 'perception', path)
  const surfaces = perception.surfaces
  if (!Array.isArray(surfaces) || !surfaces.every((value) => typeof value === 'string')) {
    throw new Error(`${path} 的 perception.surfaces 必须是字符串数组`)
  }
  if (surfaces.includes('group')) {
    throw new Error(
      `${path} 的群聊不能启用屏幕情境：群消息会被多人看见，屏幕内容一旦发出无法撤回`,
    )
  }
  const invalid = surfaces.filter((value) => value !== 'desktop' && value !== 'direct')
  if (invalid.length > 0) {
    throw new Error(`${path} 的 perception.surfaces 只能填写 desktop 或 direct：${invalid.join('、')}`)
  }
  return [...surfaces] as Array<'desktop' | 'direct'>
}

/**
 * 从配置记录中读取必需的布尔字段。
 *
 * @param record 当前配置记录。
 * @param key 字段名。
 * @param path 用于错误信息的配置文件路径或字段路径。
 * @returns 字段的布尔值。
 * @throws Error 当字段不存在或值不是布尔值时抛出。
 */
function booleanAt(record: Record<string, unknown>, key: string, path: string): boolean {
  const value = record[key]
  if (typeof value !== 'boolean') throw new Error(`${path} 的 ${key} 必须是布尔值`)
  return value
}

/**
 * 读取 TOML 文件并校验顶层结构版本。
 *
 * @param path TOML 配置文件的绝对或相对路径。
 * @returns 已解析的配置文档及其 `inner.version` 版本字符串。
 * @throws Error 当文件不可读、TOML 语法无效、顶层不是表、缺少版本或版本不受支持时抛出。
 */
function parseToml(path: string): { document: Record<string, unknown>; version: string } {
  let parsed: unknown
  try {
    parsed = TOML.parse(readFileSync(path, 'utf-8'))
  } catch (error) {
    throw new Error(`无法解析 ${path}：${error instanceof Error ? error.message : String(error)}`)
  }
  if (!isRecord(parsed)) throw new Error(`${path} 的顶层必须是 TOML 表`)
  const inner = recordAt(parsed, 'inner', path)
  const version = stringAt(inner, 'version', path)
  if (!SUPPORTED_VERSIONS.includes(version as (typeof SUPPORTED_VERSIONS)[number])) {
    throw new Error(
      `${path} 的配置版本为 ${version}，当前支持 ${SUPPORTED_VERSIONS.join(' / ')}`,
    )
  }
  return { document: parsed, version }
}

/**
 * 校验 OpenAI 兼容服务商的鉴权字段组合，并规范化鉴权名称。
 *
 * @param provider 待校验的服务商配置；方法会原地去除 `auth_name` 首尾空白。
 * @param path 用于错误信息的配置文件路径或字段路径。
 * @returns {void} 无返回值；校验通过时保留规范化后的服务商配置。
 * @throws Error 当鉴权类型与鉴权名称、API 密钥的组合不满足约束时抛出。
 * @remarks `volcengine` 私有协议不使用本组字段校验，因此直接返回。
 */
function validateProviderAuth(provider: ApiProviderConfig, path: string): void {
  if (provider.client_type !== 'openai') return
  provider.auth_name = provider.auth_name.trim()
  const hasKey = provider.api_key.trim().length > 0
  if ((provider.auth_type === 'header' || provider.auth_type === 'query') && !provider.auth_name) {
    throw new Error(`${path} 的 auth_type=${provider.auth_type} 时 auth_name 不能为空`)
  }
  if ((provider.auth_type === 'bearer' || provider.auth_type === 'none') && provider.auth_name) {
    throw new Error(`${path} 的 auth_type=${provider.auth_type} 时 auth_name 必须留空`)
  }
  if (provider.auth_type === 'none' && hasKey) {
    throw new Error(`${path} 的 auth_type=none 时 api_key 必须留空`)
  }
  if (provider.auth_type !== 'none' && !hasKey) {
    throw new Error(`${path} 的 auth_type=${provider.auth_type} 时 api_key 不能为空`)
  }
}

/**
 * 从服务商 TOML 文件解析并校验所有 API 服务商定义。
 *
 * @param path 服务商配置文件路径。
 * @returns 按文件顺序排列且名称唯一的服务商配置数组。
 * @throws Error 当文件结构、字段类型、客户端类型、鉴权字段或名称唯一性不满足约束时抛出。
 * @remarks 方法会读取文件并对每个服务商执行鉴权字段规范化。
 */
function parseProviders(path: string): ApiProviderConfig[] {
  const { document } = parseToml(path)
  const definitions = document.api_providers
  if (!Array.isArray(definitions)) throw new Error(`${path} 缺少 [[api_providers]]`)
  const providers = definitions.map((value, index) => {
    const itemPath = `${path} 的 api_providers[${index}]`
    if (!isRecord(value)) throw new Error(`${itemPath} 必须是表`)
    const clientType = stringAt(value, 'client_type', itemPath)
    // 私有语音协议只能承载语音任务；跨任务引用由运行时配置加载器继续校验，
    // 此处保留该客户端类型以便完整解析服务商文件。
    if (clientType !== 'openai' && clientType !== 'volcengine') {
      throw new Error(`${itemPath} 的 client_type 当前只支持 openai 或 volcengine`)
    }
    const authType = stringAtOr(value, 'auth_type', 'bearer', itemPath)
    if (!['bearer', 'header', 'query', 'none'].includes(authType)) {
      throw new Error(`${itemPath} 的 auth_type 配置不合法`)
    }
    const provider: ApiProviderConfig = {
      name: stringAt(value, 'name', itemPath),
      kind: stringAt(value, 'kind', itemPath),
      base_url: stringAt(value, 'base_url', itemPath),
      api_key: stringAt(value, 'api_key', itemPath),
      auth_type: authType as AuthType,
      auth_name: stringAtOr(value, 'auth_name', '', itemPath),
      client_type: clientType as ClientType,
      app_id: stringAtOr(value, 'app_id', '', itemPath),
      model_list_endpoint: stringAtOr(
        value, 'model_list_endpoint', DEFAULT_PROVIDER.model_list_endpoint, itemPath,
      ),
      default_headers: stringRecordOr(value, 'default_headers', {}, itemPath),
      default_query: stringRecordOr(value, 'default_query', {}, itemPath),
      timeout_ms: numberAt(value, 'timeout_ms', itemPath),
      max_retries: numberAtOr(value, 'max_retries', DEFAULT_PROVIDER.max_retries, itemPath),
      retry_interval_ms: numberAtOr(
        value, 'retry_interval_ms', DEFAULT_PROVIDER.retry_interval_ms, itemPath,
      ),
    }
    validateProviderAuth(provider, itemPath)
    return provider
  })
  assertUniqueNames(providers, path, 'api_providers')
  return providers
}

/**
 * 解析各生成任务的温度、令牌上限及主动任务开关。
 *
 * @param document 已解析的配置文档。
 * @param path 用于错误信息的配置文件路径或字段路径。
 * @returns 合并默认值后的生成配置。
 * @throws Error 当生成任务不是配置表、字段类型无效或仍包含已废弃的 `thinking` 字段时抛出。
 */
function parseGeneration(document: Record<string, unknown>, path: string): GenerationConfig {
  if (document.generation === undefined) return structuredClone(DEFAULT_CONFIG.generation)
  const generation = recordAt(document, 'generation', path)
  const result = structuredClone(DEFAULT_CONFIG.generation)
  for (const task of [
    'chat', 'proactive', 'summary', 'schedule', 'expression', 'vision',
    'planner', 'replyer', 'scene', 'memory',
  ] as const) {
    if (generation[task] === undefined) continue
    const taskConfig = recordAt(generation, task, `${path} 的 generation`)
    const parsed = {
      temperature: numberAtOr(
        taskConfig, 'temperature', result[task].temperature, `${path} 的 generation.${task}`,
      ),
      max_tokens: numberAtOr(
        taskConfig, 'max_tokens', result[task].max_tokens, `${path} 的 generation.${task}`,
      ),
    }
    if ('thinking' in taskConfig) {
      throw new Error(
        `${path} 的 generation.${task}.thinking 已经取消，请改用模型条目的 extra_body`,
      )
    }
    if (task === 'proactive') {
      result.proactive = {
        ...parsed,
        enabled: taskConfig.enabled === undefined
          ? result.proactive.enabled
          : booleanAt(taskConfig, 'enabled', `${path} 的 generation.proactive`),
      }
    } else {
      result[task] = parsed
    }
  }
  return result
}

/**
 * 解析单个任务的候选模型列表和轮询参数。
 *
 * @param taskRecord `model_tasks` 配置表。
 * @param task 待解析的任务名称。
 * @param path 用于错误信息的模型配置文件路径。
 * @returns 合并默认值后的任务路由配置；兼容旧版字符串模型名格式。
 * @throws Error 当候选列表、选择策略、超时或候选名称重复时抛出。
 * @remarks 旧版单字符串配置只转换为单元素 `model_list`，写回时统一使用新结构。
 */
function parseTaskRouting(
  taskRecord: Record<string, unknown>, task: ModelTask, path: string,
): TaskRoutingConfig {
  const defaults = DEFAULT_CONFIG.model_tasks[task]
  const value = taskRecord[task]
  if (value === undefined) return structuredClone(defaults)
  if (typeof value === 'string') {
    return { ...structuredClone(defaults), model_list: value ? [value] : [] }
  }
  if (!isRecord(value)) {
    throw new Error(`${path} 的 model_tasks.${task} 必须是模型名或 [model_tasks.${task}] 配置段`)
  }
  const itemPath = `${path} 的 model_tasks.${task}`
  const modelList = value.model_list
  if (!Array.isArray(modelList) || !modelList.every((item) => typeof item === 'string')) {
    throw new Error(`${itemPath} 的 model_list 必须是模型名数组`)
  }
  const strategy = stringAtOr(value, 'selection_strategy', 'sequential', itemPath)
  if (strategy !== 'sequential' && strategy !== 'random' && strategy !== 'balance') {
    throw new Error(`${itemPath} 的 selection_strategy 只能是 sequential、random 或 balance`)
  }
  // 重复候选会让顺序轮询重复命中同一模型，并使随机策略的权重失真，因此在加载期拒绝。
  const seen = new Set<string>()
  for (const name of modelList as string[]) {
    if (seen.has(name)) throw new Error(`${itemPath} 的 model_list 存在重复模型：${name}`)
    seen.add(name)
  }
  const firstTokenTimeout = numberAtOr(
    value, 'first_token_timeout_ms', defaults.first_token_timeout_ms, itemPath,
  )
  const slowThreshold = numberAtOr(
    value, 'slow_threshold_ms', defaults.slow_threshold_ms, itemPath,
  )
  if (!Number.isInteger(firstTokenTimeout) || firstTokenTimeout < 1_000) {
    throw new Error(`${itemPath} 的 first_token_timeout_ms 必须是至少 1000 的整数`)
  }
  if (!Number.isInteger(slowThreshold) || slowThreshold < 0) {
    throw new Error(`${itemPath} 的 slow_threshold_ms 必须是非负整数`)
  }
  if (slowThreshold !== 0 && slowThreshold >= firstTokenTimeout) {
    throw new Error(`${itemPath} 的 slow_threshold_ms 必须小于 first_token_timeout_ms，或设为 0`)
  }
  return {
    model_list: [...(modelList as string[])],
    selection_strategy: strategy,
    first_token_timeout_ms: firstTokenTimeout,
    slow_threshold_ms: slowThreshold,
  }
}

/**
 * 解析模型定义、任务路由和生成参数，并校验模型名称唯一性。
 *
 * @param path 模型配置文件路径。
 * @returns 模型定义、任务路由和生成配置的组合结果。
 * @throws Error 当模型文件结构、字段类型、推理解析模式或名称唯一性不满足约束时抛出。
 * @remarks 方法同时读取模型配置文件和其中声明的任务路由，不会验证服务商引用是否存在。
 */
function parseModels(
  path: string,
): { models: ModelDefinitionConfig[]; tasks: YueliConfig['model_tasks']; generation: GenerationConfig } {
  const { document } = parseToml(path)
  const taskRecord = recordAt(document, 'model_tasks', path)
  const tasks = Object.fromEntries(
    MODEL_TASKS.map((task) => [task, parseTaskRouting(taskRecord, task, path)]),
  ) as YueliConfig['model_tasks']
  const definitions = document.models
  if (!Array.isArray(definitions)) throw new Error(`${path} 缺少 [[models]]`)
  const models = definitions.map<ModelDefinitionConfig>((value, index) => {
    const itemPath = `${path} 的 models[${index}]`
    if (!isRecord(value)) throw new Error(`${itemPath} 必须是表`)
    if ('thinking' in value) {
      throw new Error(`${itemPath} 的 thinking 已经取消，请改用 extra_body`)
    }
    const extraBody = value.extra_body === undefined ? {} : recordAt(value, 'extra_body', itemPath)
    const reasoningMode = stringAtOr(value, 'reasoning_parse_mode', 'field', itemPath)
    if (reasoningMode !== 'field' && reasoningMode !== 'tag' && reasoningMode !== 'none') {
      throw new Error(`${itemPath} 的 reasoning_parse_mode 只能是 field、tag 或 none`)
    }
    // 模型级温度与输出上限可留空（null）；留空时使用任务 generation 配置。
    const modelTemperature = value.temperature === undefined
      ? null
      : numberAtOr(value, 'temperature', 0, itemPath)
    if (modelTemperature !== null && (modelTemperature < 0 || modelTemperature > 2)) {
      throw new Error(`${itemPath} 的 temperature 必须在 0 到 2 之间`)
    }
    const modelMaxTokens = value.max_tokens === undefined
      ? null
      : numberAtOr(value, 'max_tokens', 0, itemPath)
    if (modelMaxTokens !== null && (!Number.isInteger(modelMaxTokens) || modelMaxTokens < 1)) {
      throw new Error(`${itemPath} 的 max_tokens 必须是正整数`)
    }
    return {
      name: stringAt(value, 'name', itemPath),
      model_identifier: stringAt(value, 'model_identifier', itemPath),
      api_provider: stringAt(value, 'api_provider', itemPath),
      extra_body: structuredClone(extraBody),
      reasoning_parse_mode: reasoningMode,
      visual: value.visual === undefined ? false : booleanAt(value, 'visual', itemPath),
      temperature: modelTemperature,
      max_tokens: modelMaxTokens,
      price_in: numberAtOr(value, 'price_in', 0, itemPath),
      price_out: numberAtOr(value, 'price_out', 0, itemPath),
      embedding_dim: numberAtOr(value, 'embedding_dim', 0, itemPath),
    }
  })
  assertUniqueNames(models, path, 'models')
  return { models, tasks, generation: parseGeneration(document, path) }
}

/**
 * 从配置文档读取会话记忆参数，并为缺失字段合并默认值。
 *
 * @param document 已解析的配置文档。
 * @param path 用于错误信息的配置文件路径或字段路径。
 * @returns 合并默认值后的会话配置。
 * @throws Error 当 `conversation` 不是表或其字段类型无效时抛出。
 */
function parseConversation(
  document: Record<string, unknown>, path: string,
): YueliConfig['conversation'] {
  if (document.conversation === undefined) return structuredClone(DEFAULT_CONFIG.conversation)
  const conversation = recordAt(document, 'conversation', path)
  const defaults = DEFAULT_CONFIG.conversation
  return {
    working_memory_messages: numberAtOr(
      conversation, 'working_memory_messages', defaults.working_memory_messages, path,
    ),
    summarize_trigger_messages: numberAtOr(
      conversation, 'summarize_trigger_messages', defaults.summarize_trigger_messages, path,
    ),
    summarize_batch_messages: numberAtOr(
      conversation, 'summarize_batch_messages', defaults.summarize_batch_messages, path,
    ),
    session_gap_minutes: numberAtOr(
      conversation, 'session_gap_minutes', defaults.session_gap_minutes, path,
    ),
    fact_recall_limit: numberAtOr(
      conversation, 'fact_recall_limit', defaults.fact_recall_limit, path,
    ),
    recalled_episode_limit: numberAtOr(
      conversation, 'recalled_episode_limit', defaults.recalled_episode_limit, path,
    ),
    recent_episode_limit: numberAtOr(
      conversation, 'recent_episode_limit', defaults.recent_episode_limit, path,
    ),
    episode_context_limit: numberAtOr(
      conversation, 'episode_context_limit', defaults.episode_context_limit, path,
    ),
    fact_extract_trigger_messages: numberAtOr(
      conversation, 'fact_extract_trigger_messages', defaults.fact_extract_trigger_messages, path,
    ),
    fact_extract_batch_messages: numberAtOr(
      conversation, 'fact_extract_batch_messages', defaults.fact_extract_batch_messages, path,
    ),
    private_facts_in_group: conversation.private_facts_in_group === undefined
      ? defaults.private_facts_in_group
      : booleanAt(conversation, 'private_facts_in_group', path),
  }
}

/**
 * 从配置文档读取对话 Agent 参数，并为缺失字段合并默认值。
 *
 * @param document 已解析的配置文档。
 * @param path 用于错误信息的配置文件路径。
 * @returns 合并默认值后的对话 Agent 配置。
 * @throws Error 当段不是表、枚举值或数值范围无效时抛出。
 */
function parseConversationAgent(
  document: Record<string, unknown>, path: string,
): YueliConfig['conversation_agent'] {
  const defaults = DEFAULT_CONFIG.conversation_agent
  if (document.conversation_agent === undefined) return structuredClone(defaults)
  const section = recordAt(document, 'conversation_agent', path)
  const sectionPath = `${path} 的 conversation_agent`
  const mode = stringAtOr(section, 'mode', defaults.mode, sectionPath)
  if (!['off', 'shadow', 'selected_streams', 'enabled'].includes(mode)) {
    throw new Error(`${sectionPath}.mode 必须是 off、shadow、selected_streams 或 enabled`)
  }
  const streams = section.selected_streams
  if (streams !== undefined
    && (!Array.isArray(streams) || !streams.every((value) => typeof value === 'string'))) {
    throw new Error(`${sectionPath}.selected_streams 必须是字符串数组`)
  }
  const triggerMode = stringAtOr(section, 'trigger_mode', defaults.trigger_mode, sectionPath)
  if (!['signal', 'frequency', 'reply_necessity'].includes(triggerMode)) {
    throw new Error(`${sectionPath}.trigger_mode 必须是 signal、frequency 或 reply_necessity`)
  }
  const frequencyTalkValue = numberAtOr(
    section, 'frequency_talk_value', defaults.frequency_talk_value, sectionPath,
  )
  if (frequencyTalkValue <= 0 || frequencyTalkValue > 1) {
    throw new Error(`${sectionPath}.frequency_talk_value 必须在 0 到 1 之间（不含 0）`)
  }
  const replyNecessityThreshold = numberAtOr(
    section, 'reply_necessity_threshold', defaults.reply_necessity_threshold, sectionPath,
  )
  if (!Number.isInteger(replyNecessityThreshold)
    || replyNecessityThreshold < 0 || replyNecessityThreshold > 100) {
    throw new Error(`${sectionPath}.reply_necessity_threshold 必须是 0 到 100 的整数`)
  }
  const cognitiveRounds = numberAtOr(
    section, 'max_cognitive_rounds', defaults.max_cognitive_rounds, sectionPath,
  )
  if (!Number.isInteger(cognitiveRounds) || cognitiveRounds < 0 || cognitiveRounds > 4) {
    throw new Error(`${sectionPath}.max_cognitive_rounds 必须是 0 到 4 的整数`)
  }
  return {
    mode: mode as YueliConfig['conversation_agent']['mode'],
    selected_streams: streams === undefined
      ? [...defaults.selected_streams]
      : [...(streams as string[])],
    trigger_mode: triggerMode as YueliConfig['conversation_agent']['trigger_mode'],
    frequency_talk_value: frequencyTalkValue,
    reply_necessity_threshold: replyNecessityThreshold,
    max_cognitive_rounds: cognitiveRounds,
    split_replyer: section.split_replyer === undefined
      ? defaults.split_replyer
      : booleanAt(section, 'split_replyer', sectionPath),
    tool_calling: section.tool_calling === undefined
      ? defaults.tool_calling
      : booleanAt(section, 'tool_calling', sectionPath),
  }
}

/**
 * 读取数值字段并在缺失时回落默认值，随后按「大于等于下界」校验。
 *
 * @param record 所属配置表。
 * @param key 字段名。
 * @param defaults 提供回落值的默认配置。
 * @param minInclusive 允许的最小值（含）。
 * @param path 用于错误信息的配置路径。
 * @returns 配置值或默认值。
 * @throws Error 当值小于下界或不是有限数值时抛出。
 */
function nonNegativeNumberAtOr(
  record: Record<string, unknown>,
  key: string,
  defaultValue: number,
  path: string,
): number {
  const value = numberAtOr(record, key, defaultValue, path)
  if (!Number.isFinite(value) || value < 0) {
    throw new Error(`${path} 的 ${key} 必须是不小于 0 的数值`)
  }
  return value
}

/**
 * 从配置文档读取气泡拆分与打字节奏参数，并为缺失字段合并默认值。
 *
 * @param document 已解析的配置文档。
 * @param path 用于错误信息的配置文件路径。
 * @returns 合并默认值后的打字节奏配置。
 * @throws Error 当段不是表、数值范围或嵌套段无效时抛出。
 */
function parseTyping(document: Record<string, unknown>, path: string): YueliConfig['typing'] {
  const defaults = DEFAULT_CONFIG.typing
  if (document.typing === undefined) return structuredClone(defaults)
  const section = recordAt(document, 'typing', path)
  const sectionPath = `${path} 的 typing`
  const bubbleTargetChars = numberAtOr(
    section, 'bubble_target_chars', defaults.bubble_target_chars, sectionPath,
  )
  if (!Number.isInteger(bubbleTargetChars) || bubbleTargetChars < 1) {
    throw new Error(`${sectionPath}.bubble_target_chars 必须是正整数`)
  }
  const maxBubblesPerSay = numberAtOr(
    section, 'max_bubbles_per_say', defaults.max_bubbles_per_say, sectionPath,
  )
  if (!Number.isInteger(maxBubblesPerSay) || maxBubblesPerSay < 1) {
    throw new Error(`${sectionPath}.max_bubbles_per_say 必须是正整数`)
  }
  const followUp = section.follow_up === undefined
    ? undefined
    : recordAt(section, 'follow_up', sectionPath)
  const nudge = section.nudge === undefined ? undefined : recordAt(section, 'nudge', sectionPath)
  const followUpSilence = followUp === undefined
    ? defaults.follow_up.peer_silence_minutes
    : numberAtOr(
      followUp, 'peer_silence_minutes', defaults.follow_up.peer_silence_minutes, sectionPath,
    )
  if (followUpSilence <= 0) {
    throw new Error(`${sectionPath}.follow_up.peer_silence_minutes 必须大于 0`)
  }
  const nudgeSilence = nudge === undefined
    ? defaults.nudge.peer_silence_minutes
    : numberAtOr(nudge, 'peer_silence_minutes', defaults.nudge.peer_silence_minutes, sectionPath)
  if (nudgeSilence <= 0) {
    throw new Error(`${sectionPath}.nudge.peer_silence_minutes 必须大于 0`)
  }
  const nudgeMaxPerSilence = nudge === undefined
    ? defaults.nudge.max_per_silence
    : numberAtOr(nudge, 'max_per_silence', defaults.nudge.max_per_silence, sectionPath)
  if (!Number.isInteger(nudgeMaxPerSilence) || nudgeMaxPerSilence < 0) {
    throw new Error(`${sectionPath}.nudge.max_per_silence 必须是非负整数`)
  }
  return {
    bubble_target_chars: bubbleTargetChars,
    max_bubbles_per_say: maxBubblesPerSay,
    delay_enabled: section.delay_enabled === undefined
      ? defaults.delay_enabled
      : booleanAt(section, 'delay_enabled', sectionPath),
    chinese_char_seconds: nonNegativeNumberAtOr(
      section, 'chinese_char_seconds', defaults.chinese_char_seconds, sectionPath,
    ),
    latin_char_seconds: nonNegativeNumberAtOr(
      section, 'latin_char_seconds', defaults.latin_char_seconds, sectionPath,
    ),
    send_gap_seconds: nonNegativeNumberAtOr(
      section, 'send_gap_seconds', defaults.send_gap_seconds, sectionPath,
    ),
    max_delay_seconds: nonNegativeNumberAtOr(
      section, 'max_delay_seconds', defaults.max_delay_seconds, sectionPath,
    ),
    emoji_pick_seconds: nonNegativeNumberAtOr(
      section, 'emoji_pick_seconds', defaults.emoji_pick_seconds, sectionPath,
    ),
    follow_up: {
      enabled: followUp === undefined || followUp.enabled === undefined
        ? defaults.follow_up.enabled
        : booleanAt(followUp, 'enabled', sectionPath),
      peer_silence_minutes: followUpSilence,
    },
    nudge: {
      enabled: nudge === undefined || nudge.enabled === undefined
        ? defaults.nudge.enabled
        : booleanAt(nudge, 'enabled', sectionPath),
      peer_silence_minutes: nudgeSilence,
      max_per_silence: nudgeMaxPerSilence,
    },
  }
}

/**
 * 从配置文档读取表情包库管理参数，并为缺失字段合并默认值。
 *
 * @param document 已解析的配置文档。
 * @param path 用于错误信息的配置文件路径。
 * @returns 合并默认值后的表情包库配置。
 * @throws Error 当段不是表、数值范围或嵌套段无效时抛出。
 */
function parseEmoji(document: Record<string, unknown>, path: string): YueliConfig['emoji'] {
  const defaults = DEFAULT_CONFIG.emoji
  if (document.emoji === undefined) return structuredClone(defaults)
  const section = recordAt(document, 'emoji', path)
  const sectionPath = `${path} 的 emoji`
  const cleanup = section.cleanup === undefined
    ? undefined
    : recordAt(section, 'cleanup', sectionPath)
  const maxCount = numberAtOr(section, 'max_count', defaults.max_count, sectionPath)
  if (!Number.isInteger(maxCount) || maxCount < 0) {
    throw new Error(`${sectionPath}.max_count 必须是非负整数，0 表示不限`)
  }
  const checkIntervalMinutes = numberAtOr(
    section, 'check_interval_minutes', defaults.check_interval_minutes, sectionPath,
  )
  if (!Number.isInteger(checkIntervalMinutes) || checkIntervalMinutes < 1) {
    throw new Error(`${sectionPath}.check_interval_minutes 必须是正整数`)
  }
  const cleanupHours = cleanup === undefined
    ? defaults.cleanup.check_interval_hours
    : numberAtOr(cleanup, 'check_interval_hours', defaults.cleanup.check_interval_hours, sectionPath)
  if (cleanupHours <= 0) {
    throw new Error(`${sectionPath}.cleanup.check_interval_hours 必须大于 0`)
  }
  const retentionDays = cleanup === undefined
    ? defaults.cleanup.orphan_retention_days
    : numberAtOr(
      cleanup, 'orphan_retention_days', defaults.cleanup.orphan_retention_days, sectionPath,
    )
  if (!Number.isInteger(retentionDays) || retentionDays < 0) {
    throw new Error(`${sectionPath}.cleanup.orphan_retention_days 必须是非负整数`)
  }
  return {
    max_count: maxCount,
    auto_evict: section.auto_evict === undefined
      ? defaults.auto_evict
      : booleanAt(section, 'auto_evict', sectionPath),
    check_interval_minutes: checkIntervalMinutes,
    max_file_size_mb: nonNegativeNumberAtOr(
      section, 'max_file_size_mb', defaults.max_file_size_mb, sectionPath,
    ),
    content_filtration: section.content_filtration === undefined
      ? defaults.content_filtration
      : booleanAt(section, 'content_filtration', sectionPath),
    collect_enabled: section.collect_enabled === undefined
      ? defaults.collect_enabled
      : booleanAt(section, 'collect_enabled', sectionPath),
    cleanup: {
      enabled: cleanup === undefined || cleanup.enabled === undefined
        ? defaults.cleanup.enabled
        : booleanAt(cleanup, 'enabled', sectionPath),
      check_interval_hours: cleanupHours,
      orphan_retention_days: retentionDays,
    },
  }
}

/**
 * 从配置文档读取桌宠外壳开关，并为缺失字段合并默认值。
 *
 * @param document 已解析的配置文档。
 * @param path 用于错误信息的配置文件路径。
 * @returns 合并默认值后的桌宠配置。
 * @throws Error 当段不是表或 `enabled` 不是布尔值时抛出。
 */
function parseDesktopPet(document: Record<string, unknown>, path: string): YueliConfig['desktop_pet'] {
  const defaults = DEFAULT_CONFIG.desktop_pet
  if (document.desktop_pet === undefined) return structuredClone(defaults)
  const section = recordAt(document, 'desktop_pet', path)
  return {
    enabled: section.enabled === undefined
      ? defaults.enabled
      : booleanAt(section, 'enabled', `${path} 的 desktop_pet`),
  }
}

/**
 * 校验每日方向的备用主题和生成重试间隔。
 *
 * @param schedule 待校验的日程配置。
 * @param path 用于错误信息的配置路径。
 * @returns {void} 无返回值；校验通过表示方向字段满足范围和格式约束。
 * @throws Error 当主题文本或重试间隔不满足约束时抛出。
 */
function assertSchedule(schedule: YueliConfig['schedule'], path: string): void {
  if (
    !Number.isInteger(schedule.generation_retry_interval_minutes)
    || schedule.generation_retry_interval_minutes < 1
    || schedule.generation_retry_interval_minutes > 1440
  ) {
    throw new Error(`${path}.generation_retry_interval_minutes 必须是 1 到 1440 的整数`)
  }
  const theme = schedule.fallback_theme.trim()
  if (!theme || theme.length > 72) {
    throw new Error(`${path}.fallback_theme 必须是 1 到 72 个字符`)
  }
}

/**
 * 从配置文档读取日程参数，并合并默认值后执行完整校验。
 *
 * @param document 已解析的配置文档。
 * @param path 用于错误信息的配置文件路径或字段路径。
 * @returns 合并默认值且已通过校验的日程配置。
 * @throws Error 当日程段结构、字段类型或字段取值无效时抛出。
 */
function parseSchedule(
  document: Record<string, unknown>, path: string,
): YueliConfig['schedule'] {
  if (document.schedule === undefined) return structuredClone(DEFAULT_CONFIG.schedule)
  const value = recordAt(document, 'schedule', path)
  const defaults = DEFAULT_CONFIG.schedule
  const schedule: YueliConfig['schedule'] = {
    sleep_enabled: value.sleep_enabled === undefined
      ? defaults.sleep_enabled
      : booleanAt(value, 'sleep_enabled', path),
    fallback_theme: stringAtOr(value, 'fallback_theme', defaults.fallback_theme, path),
    generation_retry_interval_minutes: numberAtOr(
      value,
      'generation_retry_interval_minutes',
      defaults.generation_retry_interval_minutes,
      path,
    ),
  }
  assertSchedule(schedule, `${path} 的 schedule`)
  return schedule
}

const RETIRED_PERSONALITY_FIELDS: Record<string, string> = {
  identity: 'personality.identity 已改名为 personality.personality，请把内容挪过去。',
  behavior: 'personality.behavior 已取消：接话方式并进 reply_style。',
  attention: 'personality.attention 已取消：接话方式并进 reply_style。',
  boundaries: 'personality.boundaries 已取消：边界与事实纪律现在由固定提示词资源维护，不再可配。',
  expression_habits: 'personality.expression_habits 已取消：表达方式改由 expressions 表学习提供，不再可配。',
  proactive_expression_habits: 'personality.proactive_expression_habits 已取消：表达方式改由 expressions 表学习提供，不再可配。',
}

/**
 * 处理人格配置中已移除的字段。
 *
 * 旧版本配置里残留的退休字段**就地剪除**，随后的整目录重写会把它们写没——
 * 版本号驱动升级的全部意义就是用户不必手改 TOML。只有当配置已经声明为当前
 * 版本却仍带着退休字段时才报错：那意味着有人在升级之后又手工加了回来，
 * 静默剪除会让这次改动无声消失。
 *
 * - 现象：不区分版本一律抛错时，带退休字段的旧配置在升级入口就被拒，
 *   重写永远跑不到，用户只能自己去删数组。
 * - 原因：读取先于写入，而校验挂在读取上。
 * - 后果：擅自改回无条件抛错，会让「升级配置」重新变成手工活。
 *
 * @param personality 人格配置记录，命中退休字段时**原地删除**。
 * @param version 该配置文件声明的版本号。
 * @returns {void} 无返回值。
 * @throws Error 配置已是当前版本却仍包含退休字段时抛出。
 */
function pruneRetiredPersonalityFields(
  personality: Record<string, unknown>,
  version: string,
): void {
  for (const [field, message] of Object.entries(RETIRED_PERSONALITY_FIELDS)) {
    if (!(field in personality)) continue
    if (version === CONFIG_VERSION) throw new Error(message)
    delete personality[field]
  }
}

/**
 * 校验生日为空或为不晚于当前日期的真实日历日期。
 *
 * @param value 生日文本；空字符串表示未配置，非空值必须为 `YYYY-MM-DD`。
 * @param path 用于错误信息的配置路径。
 * @returns {void} 无返回值；空值或不晚于当前日期的合法日期视为通过。
 * @throws Error 当格式无效、日期不存在或日期晚于当前日期时抛出。
 */
function assertBirthday(value: string, path: string): void {
  if (!value) return
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new Error(`${path} 必须使用 YYYY-MM-DD 格式`)
  }
  const [year, month, day] = value.split('-').map(Number)
  const birthday = new Date(Date.UTC(year!, month! - 1, day!))
  if (
    birthday.getUTCFullYear() !== year
    || birthday.getUTCMonth() + 1 !== month
    || birthday.getUTCDate() !== day
  ) {
    throw new Error(`${path} 必须是合法日期`)
  }
  const today = new Date()
  const todayUtc = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate())
  if (birthday.getTime() > todayUtc) throw new Error(`${path} 不能晚于今天`)
}

/**
 * 校验配置条目的名称非空且在同一配置段内唯一。
 *
 * @param items 带有 `name` 字段的配置条目数组。
 * @param path 用于错误信息的配置文件路径。
 * @param section 配置段名称，用于生成字段级错误信息。
 * @returns {void} 无返回值；所有名称非空且唯一时校验通过。
 * @throws Error 当名称为空或出现重复名称时抛出。
 */
function assertUniqueNames(items: Array<{ name: string }>, path: string, section: string): void {
  const names = new Set<string>()
  for (const item of items) {
    if (!item.name) throw new Error(`${path} 的 ${section}.name 不能为空`)
    if (names.has(item.name)) throw new Error(`${path} 的 ${section} 存在重复名称：${item.name}`)
    names.add(item.name)
  }
}

/**
 * 校验模型到服务商以及任务到模型的双向配置引用。
 *
 * @param models 模型定义数组。
 * @param tasks 各任务的候选模型路由。
 * @param providers API 服务商定义数组。
 * @param modelsPath 模型配置文件路径，用于任务引用错误信息。
 * @param providersPath 服务商配置文件路径，用于模型引用错误信息。
 * @returns {void} 无返回值；所有双向引用均可解析时校验通过。
 * @throws Error 当模型引用不存在的服务商，或任务引用不存在的模型时抛出。
 */
function assertReferencesResolve(
  models: ModelDefinitionConfig[],
  tasks: YueliConfig['model_tasks'],
  providers: ApiProviderConfig[],
  modelsPath: string,
  providersPath: string,
): void {
  for (const model of models) {
    if (!providers.some((provider) => provider.name === model.api_provider)) {
      throw new Error(
        `${providersPath} 中不存在模型 ${model.name} 引用的 API 厂商：${model.api_provider}`,
      )
    }
  }
  for (const task of MODEL_TASKS) {
    for (const name of tasks[task].model_list) {
      if (!models.some((model) => model.name === name)) {
        throw new Error(`${modelsPath} 的 model_tasks.${task} 引用了不存在的模型：${name}`)
      }
    }
  }
}

/**
 * 读取四个拆分 TOML 文件，合并为运行时配置并执行跨文件引用校验。
 *
 * @param directory 配置目录，必须包含 `providers.toml`、`models.toml`、`bot.toml` 和 `features.toml`。
 * @returns 合并默认值、完成字段校验并通过引用完整性检查的运行时配置。
 * @throws Error 当任一配置文件无法读取、结构无效、字段越界或跨文件引用不一致时抛出。
 * @remarks 方法只读取配置，不写入文件；解析过程中会创建独立的默认配置副本。
 */
function readSplitConfig(directory: string): YueliConfig {
  const providersPath = join(directory, 'providers.toml')
  const modelsPath = join(directory, 'models.toml')
  const botPath = join(directory, 'bot.toml')
  const featuresPath = join(directory, 'features.toml')

  // 先解析模型与服务商并校验交叉引用，避免将无法路由的配置继续合并到运行时。
  const providers = parseProviders(providersPath)
  const { models, tasks, generation } = parseModels(modelsPath)
  assertReferencesResolve(models, tasks, providers, modelsPath, providersPath)

  // bot.toml 同时承载身份、群聊、人格和会话参数；@ 必回属于用户选择，必须显式配置。
  const { document: botDocument, version: botVersion } = parseToml(botPath)
  const bot = recordAt(botDocument, 'bot', botPath)
  const aliases = bot.aliases ?? DEFAULT_CONFIG.bot.aliases
  if (!Array.isArray(aliases) || !aliases.every((value) => typeof value === 'string')) {
    throw new Error(`${botPath} 的 bot.aliases 必须是字符串数组`)
  }
  const botName = stringAt(bot, 'name', botPath).trim()
  const normalizedAliases = aliases.map((value) => value.trim())
  if (!botName) throw new Error(`${botPath} 的 bot.name 不能为空`)
  if (normalizedAliases.some((value) => !value)) {
    throw new Error(`${botPath} 的 bot.aliases 不能包含空字符串`)
  }
  if (normalizedAliases.includes(botName)) {
    throw new Error(`${botPath} 的 bot.aliases 不要重复 bot.name`)
  }
  if (new Set(normalizedAliases).size !== normalizedAliases.length) {
    throw new Error(`${botPath} 的 bot.aliases 不能包含重复别名`)
  }
  const groupChat = recordAt(botDocument, 'group_chat', botPath)
  const atMentionMustReply = booleanAt(groupChat, 'at_mention_must_reply', botPath)
  const nameMentionProbability = numberAtOr(
    groupChat,
    'name_mention_probability',
    DEFAULT_CONFIG.group_chat.name_mention_probability,
    botPath,
  )
  if (nameMentionProbability < 0 || nameMentionProbability > 1) {
    throw new Error(`${botPath} 的 group_chat.name_mention_probability 必须在 0 到 1 之间`)
  }
  const presenceDecayStrength = numberAtOr(
    groupChat,
    'presence_decay_strength',
    DEFAULT_CONFIG.group_chat.presence_decay_strength,
    botPath,
  )
  if (presenceDecayStrength < 0 || presenceDecayStrength > 20) {
    throw new Error(`${botPath} 的 group_chat.presence_decay_strength 必须在 0 到 20 之间`)
  }
  const personaWeight = numberAtOr(
    groupChat,
    'persona_weight',
    DEFAULT_CONFIG.group_chat.persona_weight,
    botPath,
  )
  if (personaWeight < 0 || personaWeight > 1) {
    throw new Error(`${botPath} 的 group_chat.persona_weight 必须在 0 到 1 之间`)
  }
  const replyWindowMinutes = numberAtOr(
    groupChat,
    'reply_window_minutes',
    DEFAULT_CONFIG.group_chat.reply_window_minutes,
    botPath,
  )
  if (!Number.isInteger(replyWindowMinutes) || replyWindowMinutes < 1) {
    throw new Error(`${botPath} 的 group_chat.reply_window_minutes 必须是正整数`)
  }
  const maxRepliesInWindow = numberAtOr(
    groupChat,
    'max_replies_in_window',
    DEFAULT_CONFIG.group_chat.max_replies_in_window,
    botPath,
  )
  if (!Number.isInteger(maxRepliesInWindow) || maxRepliesInWindow < 0) {
    throw new Error(`${botPath} 的 group_chat.max_replies_in_window 必须是非负整数`)
  }
  const reactionsEnabled = groupChat.reactions_enabled === undefined
    ? DEFAULT_CONFIG.group_chat.reactions_enabled
    : booleanAt(groupChat, 'reactions_enabled', botPath)
  const pokesEnabled = groupChat.pokes_enabled === undefined
    ? DEFAULT_CONFIG.group_chat.pokes_enabled
    : booleanAt(groupChat, 'pokes_enabled', botPath)
  const selfStartedTopics = groupChat.self_started_topics === undefined
    ? DEFAULT_CONFIG.group_chat.self_started_topics
    : booleanAt(groupChat, 'self_started_topics', botPath)
  const sceneRefreshMessages = numberAtOr(
    groupChat,
    'scene_refresh_messages',
    DEFAULT_CONFIG.group_chat.scene_refresh_messages,
    botPath,
  )
  if (!Number.isInteger(sceneRefreshMessages) || sceneRefreshMessages < 0) {
    throw new Error(`${botPath} 的 group_chat.scene_refresh_messages 必须是非负整数`)
  }
  const personality = recordAt(botDocument, 'personality', botPath)
  pruneRetiredPersonalityFields(personality, botVersion)
  const conversation = parseConversation(botDocument, botPath)
  const conversationAgent = parseConversationAgent(botDocument, botPath)
  const typing = parseTyping(botDocument, botPath)
  const schedule = parseSchedule(botDocument, botPath)
  const emoji = parseEmoji(botDocument, botPath)
  const desktopPet = parseDesktopPet(botDocument, botPath)
  const toneVariants = personality.tone_variants
  if (!Array.isArray(toneVariants) || !toneVariants.every((value) => typeof value === 'string')) {
    throw new Error(`${botPath} 的 personality.tone_variants 必须是字符串数组`)
  }
  const toneProbability = numberAt(personality, 'tone_probability', botPath)
  if (toneProbability < 0 || toneProbability > 1) {
    throw new Error(`${botPath} 的 personality.tone_probability 必须在 0 到 1 之间`)
  }
  const birthday = stringAt(personality, 'birthday', botPath)
  assertBirthday(birthday, `${botPath} 的 personality.birthday`)

  // features.toml 的任务开关必须与模型路由分开读取，避免旧配置迁移时互相覆盖。
  const { document: features } = parseToml(featuresPath)
  const tts = recordAt(features, 'tts', featuresPath)
  const vision = recordAt(features, 'vision', featuresPath)
  const vector = recordAt(features, 'vector', featuresPath)
  const advanced = recordAt(features, 'advanced', featuresPath)
  const developer = features.developer === undefined
    ? null
    : recordAt(features, 'developer', featuresPath)
  const ttsFormat = stringAt(tts, 'format', featuresPath)
  if (!['mp3', 'wav', 'opus'].includes(ttsFormat)) {
    throw new Error(`${featuresPath} 的 tts.format 必须是 mp3、wav 或 opus`)
  }

  return {
    bot: {
      name: botName,
      aliases: normalizedAliases,
      user_nickname: stringAt(bot, 'user_nickname', botPath),
      relationship: stringAt(bot, 'relationship', botPath),
    },
    group_chat: {
      at_mention_must_reply: atMentionMustReply,
      name_mention_probability: nameMentionProbability,
      presence_decay_strength: presenceDecayStrength,
      persona_weight: personaWeight,
      reply_window_minutes: replyWindowMinutes,
      max_replies_in_window: maxRepliesInWindow,
      reactions_enabled: reactionsEnabled,
      pokes_enabled: pokesEnabled,
      self_started_topics: selfStartedTopics,
      scene_refresh_messages: sceneRefreshMessages,
    },
    schedule,
    personality: {
      birthday,
      personality: stringAt(personality, 'personality', botPath),
      reply_style: stringAt(personality, 'reply_style', botPath),
      tone_probability: toneProbability,
      tone_variants: [...toneVariants] as string[],
    },
    conversation,
    conversation_agent: conversationAgent,
    typing,
    emoji,
    desktop_pet: desktopPet,
    generation,
    api_providers: providers,
    models,
    model_tasks: tasks,
    tts: {
      enabled: booleanAt(tts, 'enabled', featuresPath),
      voice: stringAt(tts, 'voice', featuresPath),
      format: ttsFormat as YueliConfig['tts']['format'],
      speed: numberAt(tts, 'speed', featuresPath),
      cluster: stringAtOr(tts, 'cluster', DEFAULT_CONFIG.tts.cluster, featuresPath),
    },
    vision: {
      enabled: booleanAt(vision, 'enabled', featuresPath),
      chat_image_enabled: vision.chat_image_enabled === undefined
        ? DEFAULT_CONFIG.vision.chat_image_enabled
        : booleanAt(vision, 'chat_image_enabled', featuresPath),
      fullscreen_silent: booleanAt(vision, 'fullscreen_silent', featuresPath),
      capture_mode: captureModeAt(vision, featuresPath),
    },
    perception: {
      surfaces: perceptionSurfacesAt(features, featuresPath),
    },
    vector: {
      enabled: booleanAt(vector, 'enabled', featuresPath),
    },
    memory_feedback: parseMemoryFeedback(features, featuresPath),
    log: parseLog(features, featuresPath),
    advanced: {
      https_proxy: stringAt(advanced, 'https_proxy', featuresPath),
    },
    developer: {
      enabled: developer === null
        ? DEFAULT_CONFIG.developer.enabled
        : booleanAt(developer, 'enabled', featuresPath),
    },
  }
}

/**
 * 解析日志配置中的库名抑制列表。
 *
 * @param value 待解析的未知 TOML 值。
 * @param path 用于错误信息的配置文件路径。
 * @returns 字符串数组的浅拷贝，避免调用方持有 TOML 解析结果的可变引用。
 * @throws Error 当值不是字符串数组时抛出。
 */
function parseSuppressLibraries(value: unknown, path: string): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === 'string')) {
    throw new Error(`${path} 的 log.suppress_libraries 必须是字符串数组`)
  }
  return [...(value as string[])]
}

/**
 * 读取「键和值都是字符串」的 TOML 表，缺失时返回默认值。
 *
 * @param record 所属配置表。
 * @param key 字段名。
 * @param defaultValue 字段缺失时使用的默认映射。
 * @param path 用于错误信息的配置路径。
 * @returns 新建的字符串映射；不持有 TOML 解析结果的可变引用。
 * @throws Error 当字段存在但不是表，或包含非字符串值时抛出。
 */
function stringRecordOr(
  record: Record<string, unknown>,
  key: string,
  defaultValue: Record<string, string>,
  path: string,
): Record<string, string> {
  if (record[key] === undefined) return { ...defaultValue }
  const table = recordAt(record, key, path)
  const result: Record<string, string> = {}
  for (const [name, value] of Object.entries(table)) {
    if (typeof value !== 'string') {
      throw new Error(`${path} 的 ${key}.${name} 必须是字符串`)
    }
    result[name] = value
  }
  return result
}

/**
 * 解析日志配置段，并为缺失字段合并默认值。
 *
 * @param features 已解析的功能配置文档。
 * @param path 用于错误信息的配置文件路径。
 * @returns 完整的日志运行配置。
 * @throws Error 当日志配置段、枚举值、保留数量或列表字段类型无效时抛出。
 * @remarks 方法返回新的默认配置副本，不修改 TOML 解析结果。
 */
function parseLog(features: Record<string, unknown>, path: string): YueliConfig['log'] {
  const fallback = structuredClone(DEFAULT_CONFIG.log)
  if (features.log === undefined) return fallback
  const log = recordAt(features, 'log', path)
  /**
   * 从日志配置读取受限字符串枚举，并在字段缺失时返回默认值。
   *
   * @param key 日志配置字段名。
   * @param allowed 允许的字符串枚举值集合。
   * @param defaultValue 字段缺失时使用的默认枚举值。
   * @returns {T} 配置中的合法枚举值或默认值。
   * @throws {Error} 字段存在但不是字符串，或字符串不在允许集合中时抛出。
   */
  const enumAt = <T extends string>(key: string, allowed: readonly T[], defaultValue: T): T => {
    const value = stringAtOr(log, key, defaultValue, `${path} 的 log`)
    if (!allowed.includes(value as T)) {
      throw new Error(`${path} 的 log.${key} 必须是 ${allowed.join('、')}`)
    }
    return value as T
  }
  const levels: Record<string, string> = {}
  // 库级别覆盖采用独立记录，避免把 TOML 表中的未知对象直接暴露给日志初始化代码。
  if (log.library_levels !== undefined) {
    const table = recordAt(log, 'library_levels', `${path} 的 log`)
    for (const [name, value] of Object.entries(table)) {
      if (typeof value !== 'string') {
        throw new Error(`${path} 的 log.library_levels.${name} 必须是字符串`)
      }
      levels[name] = value
    }
  }
  const eventRetentionCount = numberAtOr(
    log, 'event_retention_count', fallback.event_retention_count, `${path} 的 log`,
  )
  const eventRetentionHours = numberAtOr(
    log, 'event_retention_hours', fallback.event_retention_hours, `${path} 的 log`,
  )
  // 保留策略在加载期拒绝非法值，防止运行时清理任务执行无界查询或立即清空数据。
  if (!Number.isInteger(eventRetentionCount) || eventRetentionCount < 1) {
    throw new Error(`${path} 的 log.event_retention_count 必须是正整数`)
  }
  if (!Number.isInteger(eventRetentionHours) || eventRetentionHours < 0) {
    throw new Error(`${path} 的 log.event_retention_hours 必须是非负整数`)
  }
  return {
    level: stringAtOr(log, 'level', fallback.level, `${path} 的 log`),
    console_level: stringAtOr(log, 'console_level', fallback.console_level, `${path} 的 log`),
    file_level: stringAtOr(log, 'file_level', fallback.file_level, `${path} 的 log`),
    level_style: enumAt('level_style', ['lite', 'compact', 'full'] as const, fallback.level_style),
    color_scope: enumAt('color_scope', ['none', 'title', 'full'] as const, fallback.color_scope),
    date_format: stringAtOr(log, 'date_format', fallback.date_format, `${path} 的 log`),
    to_file: log.to_file === undefined
      ? fallback.to_file
      : booleanAt(log, 'to_file', `${path} 的 log`),
    file_max_bytes: numberAtOr(log, 'file_max_bytes', fallback.file_max_bytes, `${path} 的 log`),
    max_files: numberAtOr(log, 'max_files', fallback.max_files, `${path} 的 log`),
    cleanup_days: numberAtOr(log, 'cleanup_days', fallback.cleanup_days, `${path} 的 log`),
    library_levels: log.library_levels === undefined ? fallback.library_levels : levels,
    suppress_libraries: log.suppress_libraries === undefined
      ? fallback.suppress_libraries
      : parseSuppressLibraries(log.suppress_libraries, path),
    request_snapshots: log.request_snapshots === undefined
      ? fallback.request_snapshots
      : booleanAt(log, 'request_snapshots', `${path} 的 log`),
    max_snapshot_files: numberAtOr(
      log, 'max_snapshot_files', fallback.max_snapshot_files, `${path} 的 log`,
    ),
    prompt_records: log.prompt_records === undefined
      ? fallback.prompt_records
      : booleanAt(log, 'prompt_records', `${path} 的 log`),
    max_prompt_records_per_task: numberAtOr(
      log, 'max_prompt_records_per_task', fallback.max_prompt_records_per_task, `${path} 的 log`,
    ),
    event_retention_count: eventRetentionCount,
    event_retention_hours: eventRetentionHours,
  }
}

/**
 * 解析反馈纠错（N4）链路配置段，并为缺失字段合并默认值。
 *
 * @param features 已解析的功能配置文档。
 * @param path 用于错误信息的配置文件路径。
 * @returns 完整的反馈纠错运行配置。
 * @throws Error 当配置段不是表、布尔字段类型无效或数值越界时抛出。
 * @remarks 该段由 1.5.0 引入：旧版本文件没有它，按默认值补齐后由整目录重写写回。
 */
function parseMemoryFeedback(
  features: Record<string, unknown>, path: string,
): YueliConfig['memory_feedback'] {
  const fallback = structuredClone(DEFAULT_CONFIG.memory_feedback)
  if (features.memory_feedback === undefined) return fallback
  const section = recordAt(features, 'memory_feedback', path)
  const sectionPath = `${path} 的 memory_feedback`
  const windowHours = numberAtOr(section, 'window_hours', fallback.window_hours, sectionPath)
  if (windowHours <= 0) {
    throw new Error(`${sectionPath}.window_hours 必须大于 0`)
  }
  const autoApplyThreshold = numberAtOr(
    section, 'auto_apply_threshold', fallback.auto_apply_threshold, sectionPath,
  )
  if (autoApplyThreshold < 0 || autoApplyThreshold > 1) {
    throw new Error(`${sectionPath}.auto_apply_threshold 必须在 0 到 1 之间`)
  }
  const checkIntervalMinutes = numberAtOr(
    section, 'check_interval_minutes', fallback.check_interval_minutes, sectionPath,
  )
  if (!Number.isInteger(checkIntervalMinutes) || checkIntervalMinutes < 1) {
    throw new Error(`${sectionPath}.check_interval_minutes 必须是正整数`)
  }
  const batchSize = numberAtOr(section, 'batch_size', fallback.batch_size, sectionPath)
  if (!Number.isInteger(batchSize) || batchSize < 1) {
    throw new Error(`${sectionPath}.batch_size 必须是正整数`)
  }
  const maxFeedbackMessages = numberAtOr(
    section, 'max_feedback_messages', fallback.max_feedback_messages, sectionPath,
  )
  if (!Number.isInteger(maxFeedbackMessages) || maxFeedbackMessages < 1) {
    throw new Error(`${sectionPath}.max_feedback_messages 必须是正整数`)
  }
  const reconcileIntervalMinutes = numberAtOr(
    section, 'reconcile_interval_minutes', fallback.reconcile_interval_minutes, sectionPath,
  )
  if (!Number.isInteger(reconcileIntervalMinutes) || reconcileIntervalMinutes < 1) {
    throw new Error(`${sectionPath}.reconcile_interval_minutes 必须是正整数`)
  }
  const reconcileBatchSize = numberAtOr(
    section, 'reconcile_batch_size', fallback.reconcile_batch_size, sectionPath,
  )
  if (!Number.isInteger(reconcileBatchSize) || reconcileBatchSize < 1) {
    throw new Error(`${sectionPath}.reconcile_batch_size 必须是正整数`)
  }
  return {
    enabled: section.enabled === undefined
      ? fallback.enabled
      : booleanAt(section, 'enabled', sectionPath),
    window_hours: windowHours,
    check_interval_minutes: checkIntervalMinutes,
    batch_size: batchSize,
    auto_apply_threshold: autoApplyThreshold,
    max_feedback_messages: maxFeedbackMessages,
    prefilter_enabled: section.prefilter_enabled === undefined
      ? fallback.prefilter_enabled
      : booleanAt(section, 'prefilter_enabled', sectionPath),
    mark_enabled: section.mark_enabled === undefined
      ? fallback.mark_enabled
      : booleanAt(section, 'mark_enabled', sectionPath),
    hard_filter_enabled: section.hard_filter_enabled === undefined
      ? fallback.hard_filter_enabled
      : booleanAt(section, 'hard_filter_enabled', sectionPath),
    profile_refresh_enabled: section.profile_refresh_enabled === undefined
      ? fallback.profile_refresh_enabled
      : booleanAt(section, 'profile_refresh_enabled', sectionPath),
    profile_force_refresh_on_read: section.profile_force_refresh_on_read === undefined
      ? fallback.profile_force_refresh_on_read
      : booleanAt(section, 'profile_force_refresh_on_read', sectionPath),
    episode_rebuild_enabled: section.episode_rebuild_enabled === undefined
      ? fallback.episode_rebuild_enabled
      : booleanAt(section, 'episode_rebuild_enabled', sectionPath),
    episode_query_block_enabled: section.episode_query_block_enabled === undefined
      ? fallback.episode_query_block_enabled
      : booleanAt(section, 'episode_query_block_enabled', sectionPath),
    reconcile_interval_minutes: reconcileIntervalMinutes,
    reconcile_batch_size: reconcileBatchSize,
  }
}

/**
 * 旧版扁平配置中的连接字段集合，用于迁移为一条 API 服务商记录。
 *
 * @remarks 该接口只描述迁移中间值，不代表当前拆分配置文件的完整服务商结构。
 */
interface LegacyConnection {
  providerName: string
  kind: string
  base_url: string
  api_key: string
  auth_type: AuthType
  auth_name: string
  client_type: ClientType
  app_id: string
  timeout_ms: number
  max_retries: number
  retry_interval_ms: number
}

/**
 * 从旧版扁平配置段提取服务商连接信息，并为缺失数值字段合并已有默认值。
 *
 * @param section 旧版配置中的连接字段记录。
 * @param providerName 迁移后服务商的名称。
 * @param fallback 同一旧配置中可复用的连接默认值；不存在时使用全局服务商默认值。
 * @returns 迁移用的连接信息对象。
 * @remarks 方法仅读取输入，不写文件，也不会校验 API 密钥是否完整。
 */
function legacyConnection(
  section: Record<string, unknown>, providerName: string, fallback: LegacyConnection | null,
): LegacyConnection {
  return {
    providerName,
    kind: typeof section.provider === 'string' ? section.provider : 'openai',
    base_url: typeof section.base_url === 'string' ? section.base_url : '',
    api_key: typeof section.api_key === 'string' ? section.api_key : '',
    auth_type: 'bearer',
    auth_name: '',
    client_type: section.client_type === 'volcengine' ? 'volcengine' : 'openai',
    app_id: typeof section.app_id === 'string' ? section.app_id : '',
    timeout_ms: typeof section.timeout_ms === 'number'
      ? section.timeout_ms : fallback?.timeout_ms ?? DEFAULT_PROVIDER.timeout_ms,
    max_retries: typeof section.max_retries === 'number'
      ? section.max_retries : fallback?.max_retries ?? DEFAULT_PROVIDER.max_retries,
    retry_interval_ms: typeof section.retry_interval_ms === 'number'
      ? section.retry_interval_ms : fallback?.retry_interval_ms ?? DEFAULT_PROVIDER.retry_interval_ms,
  }
}

/**
 * 将旧版按任务保存地址和密钥的扁平配置迁移为服务商、模型和任务路由三层结构。
 *
 * @param path 旧版 TOML 配置文件路径。
 * @returns 按当前配置结构生成的运行时配置；每个已配置任务最多生成一个候选模型。
 * @throws Error 当旧配置无法读取、TOML 结构无效或包含已移除字段时抛出。
 * @remarks 方法只构造内存配置，不会写回旧文件；迁移写入由调用方负责。
 */
function readLegacyConfig(path: string): YueliConfig {
  let parsed: unknown
  try {
    parsed = TOML.parse(readFileSync(path, 'utf-8'))
  } catch (error) {
    throw new Error(`旧配置 ${path} 解析失败，未执行迁移：${error instanceof Error ? error.message : String(error)}`)
  }
  if (!isRecord(parsed)) throw new Error(`旧配置 ${path} 的顶层必须是 TOML 表`)
  const config = cloneDefaults()
  for (const section of ['bot', 'personality', 'conversation', 'generation', 'advanced'] as const) {
    const value = parsed[section]
    if (value === undefined) continue
    if (!isRecord(value)) throw new Error(`旧配置 ${path} 的 [${section}] 必须是表`)
    if (section === 'personality') pruneRetiredPersonalityFields(value, '')
    Object.assign(config[section], value)
  }

  const llm = isRecord(parsed.llm) ? parsed.llm : {}
  const tts = isRecord(parsed.tts) ? parsed.tts : {}
  const vision = isRecord(parsed.vision) ? parsed.vision : {}
  const vector = isRecord(parsed.vector) ? parsed.vector : {}

  const chat = legacyConnection(llm, '主力', null)
  const providers: ApiProviderConfig[] = []
  const models: ModelDefinitionConfig[] = []
  const tasks = structuredClone(DEFAULT_CONFIG.model_tasks)

  /**
   * 将迁移连接加入服务商列表，并返回其稳定名称。
   *
   * @param connection 旧配置解析出的服务商连接；名称相同时复用已有记录。
   * @returns {string} 连接对应的服务商名称。
   * @remarks 仅在名称尚未出现时写入 providers，不复制同名连接，避免迁移后模型引用分裂。
   */
  const pushProvider = (connection: LegacyConnection): string => {
    if (!providers.some((provider) => provider.name === connection.providerName)) {
      const { providerName, ...rest } = connection
      providers.push({
        model_list_endpoint: DEFAULT_PROVIDER.model_list_endpoint,
        default_headers: {},
        default_query: {},
        name: providerName,
        ...rest,
      })
    }
    return connection.providerName
  }
  pushProvider(chat)
  if ('thinking' in llm) {
    throw new Error('旧配置的 llm.thinking 已经取消，请迁移到模型条目的 extra_body')
  }
  models.push({
    name: 'chat',
    model_identifier: typeof llm.model === 'string' ? llm.model : '',
    api_provider: chat.providerName,
    extra_body: {},
    reasoning_parse_mode: 'field',
    visual: false, temperature: null, max_tokens: null, price_in: 0, price_out: 0,
    embedding_dim: 0,
  })
  tasks.chat = { ...tasks.chat, model_list: ['chat'], selection_strategy: 'sequential' }

  // 旧配置里 vision/vector 的地址留空就意味着复用对话连接，这里如实还原成
  // 「同一个 api_provider」而不是复制一份地址，避免改一处漏一处。
  if (typeof vision.model === 'string' && vision.model) {
    const separate = Boolean(vision.base_url || vision.api_key)
    const connection = separate ? legacyConnection(vision, '视觉', chat) : chat
    models.push({
      name: 'vision', model_identifier: vision.model,
      api_provider: pushProvider(connection), extra_body: {},
      reasoning_parse_mode: 'field',
      visual: true, temperature: null, max_tokens: null, price_in: 0, price_out: 0,
      embedding_dim: 0,
    })
    tasks.vision = { ...tasks.vision, model_list: ['vision'], selection_strategy: 'sequential' }
  }
  if (typeof tts.model === 'string' || typeof tts.voice === 'string') {
    const connection = legacyConnection(tts, '语音', chat)
    connection.kind = connection.client_type === 'volcengine' ? 'volcengine' : 'openai'
    models.push({
      name: 'tts', model_identifier: typeof tts.model === 'string' ? tts.model : '',
      api_provider: pushProvider(connection), extra_body: {},
      reasoning_parse_mode: 'none',
      visual: false, temperature: null, max_tokens: null, price_in: 0, price_out: 0,
      embedding_dim: 0,
    })
    tasks.tts = { ...tasks.tts, model_list: ['tts'], selection_strategy: 'sequential' }
  }
  if (typeof vector.embedding_model === 'string' && vector.embedding_model) {
    const separate = Boolean(vector.embedding_base_url || vector.embedding_api_key)
    const connection = separate
      ? legacyConnection({
        base_url: vector.embedding_base_url, api_key: vector.embedding_api_key,
      }, '向量', chat)
      : chat
    models.push({
      name: 'embedding', model_identifier: vector.embedding_model,
      api_provider: pushProvider(connection), extra_body: {},
      reasoning_parse_mode: 'none',
      visual: false, temperature: null, max_tokens: null, price_in: 0, price_out: 0,
      embedding_dim: typeof vector.embedding_dim === 'number' ? vector.embedding_dim : 1536,
    })
    tasks.embedding = {
      ...tasks.embedding, model_list: ['embedding'], selection_strategy: 'sequential',
    }
  }

  config.api_providers = providers
  config.models = models
  config.model_tasks = tasks
  if (typeof tts.enabled === 'boolean') config.tts.enabled = tts.enabled
  if (typeof tts.voice === 'string') config.tts.voice = tts.voice
  if (tts.format === 'mp3' || tts.format === 'wav' || tts.format === 'opus') {
    config.tts.format = tts.format
  }
  if (typeof tts.speed === 'number') config.tts.speed = tts.speed
  if (typeof tts.cluster === 'string') config.tts.cluster = tts.cluster
  if (typeof vision.enabled === 'boolean') config.vision.enabled = vision.enabled
  if (typeof vision.fullscreen_silent === 'boolean') {
    config.vision.fullscreen_silent = vision.fullscreen_silent
  }
  if (vision.capture_mode === 'window' || vision.capture_mode === 'screen') {
    config.vision.capture_mode = vision.capture_mode
  }
  if (typeof vector.enabled === 'boolean') config.vector.enabled = vector.enabled
  return config
}

/**
 * 判断配置目录中的任一文件是否仍使用旧结构版本。
 *
 * @param directory 已存在的拆分配置目录。
 * @returns 任一配置文件版本不是当前版本时返回 `true`，否则返回 `false`。
 * @throws Error 当任一配置文件无法解析或缺少版本字段时抛出。
 */
function directoryIsStale(directory: string): boolean {
  return CONFIG_FILES.some((name) => parseToml(join(directory, name)).version !== CONFIG_VERSION)
}

/**
 * 读取拆分配置目录，并在发现旧版本或旧版单文件配置时执行迁移。
 *
 * @param directory 当前拆分配置目录路径。
 * @param legacyPath 可选的旧版单文件配置路径；仅在拆分目录不存在时参与迁移。
 * @returns 合并默认值且通过完整校验的运行时配置。
 * @throws Error 当目录类型、文件完整性、配置语法或字段约束不满足要求时抛出。
 * @remarks 迁移成功后写入四个当前版本文件，旧版文件保持不变；已存在的旧版本目录会原地升级。
 */
export function readConfigDirectory(directory: string, legacyPath?: string): YueliConfig {
  if (!existsSync(directory)) {
    if (legacyPath && existsSync(legacyPath)) {
      const migrated = readLegacyConfig(legacyPath)
      writeConfigDirectory(directory, migrated)
      console.log(`[config] 已把旧配置迁移到 ${directory}，原文件 ${legacyPath} 保留`)
      return migrated
    }
    return cloneDefaults()
  }
  if (!statSync(directory).isDirectory()) throw new Error(`${directory} 存在，但不是配置目录`)
  const missing = CONFIG_FILES.filter((name) => !existsSync(join(directory, name)))
  if (missing.length > 0) throw new Error(`${directory} 缺少配置文件：${missing.join('、')}`)
  const config = readSplitConfig(directory)
  if (directoryIsStale(directory)) {
    writeConfigDirectory(directory, config)
    console.log(`[config] ${directory} 已升级到 ${CONFIG_VERSION}`)
  }
  return config
}

/**
 * 判断配置是否具备启动对话任务所需的最小条件。
 *
 * @param cfg 待检查的运行时配置。
 * @returns Bot 名称非空且对话任务存在同时具备模型 ID、服务商和 API 密钥的候选时返回 `true`。
 * @remarks 方法只读取配置，不抛出字段校验异常；完整结构校验由 {@link assertConfigConsistent} 负责。
 */
export function configIsComplete(cfg: YueliConfig): boolean {
  if (!cfg.bot.name.trim()) return false
  return cfg.model_tasks.chat.model_list.some((name) => {
    const model = cfg.models.find((candidate) => candidate.name === name)
    if (!model || !model.model_identifier.trim()) return false
    const provider = cfg.api_providers.find((candidate) => candidate.name === model.api_provider)
    return Boolean(provider && provider.api_key.trim())
  })
}

/**
 * 将字符串编码为 TOML 基本字符串。
 *
 * @param value 待编码的字符串。
 * @returns 包含双引号并完成反斜杠、引号和控制字符转义的 TOML 字符串。
 */
function tomlString(value: string): string {
  const escaped = value
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\r')
    .replace(/\t/g, '\\t')
  return `"${escaped}"`
}

/**
 * 将字符串、数字或布尔值编码为 TOML 标量。
 *
 * @param value 待编码的 TOML 标量。
 * @returns 可直接写入配置文件的 TOML 文本。
 */
function tomlValue(value: string | number | boolean): string {
  if (typeof value === 'boolean' || typeof value === 'number') return String(value)
  return tomlString(value)
}

/**
 * 将可递归的键值对象编码为 TOML 内联表。
 *
 * @param value 待编码的对象，嵌套值必须属于支持的 TOML 类型。
 * @param path 当前字段路径，用于报告嵌套值错误。
 * @returns TOML 内联表文本。
 * @throws Error 当对象包含不支持的值类型时抛出。
 */
function tomlObject(value: Record<string, unknown>, path: string): string {
  const entries = Object.entries(value).map(([key, item]) => {
    const encodedKey = /^[A-Za-z0-9_-]+$/.test(key) ? key : tomlString(key)
    return `${encodedKey} = ${tomlExtraValue(item, `${path}.${key}`)}`
  })
  return `{ ${entries.join(', ')} }`
}

/**
 * 编码模型扩展请求体中的递归 TOML 值。
 *
 * @param value 待编码的未知值。
 * @param path 当前字段路径，用于报告不支持的类型。
 * @returns 对应的 TOML 标量、数组或内联表文本。
 * @throws Error 当值不是字符串、有限数字、布尔值、数组或对象时抛出。
 */
function tomlExtraValue(value: unknown, path: string): string {
  if (typeof value === 'string' || typeof value === 'boolean') return tomlValue(value)
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  if (Array.isArray(value)) {
    return `[${value.map((item, index) => tomlExtraValue(item, `${path}[${index}]`)).join(', ')}]`
  }
  if (isRecord(value)) return tomlObject(value, path)
  throw new Error(`${path} 只能包含字符串、数字、布尔值、数组或对象`)
}

/**
 * 将多行文本编码为 TOML 三单引号字符串。
 *
 * @param value 待编码的多行文本。
 * @param field 字段名，用于报告分隔符冲突。
 * @returns TOML 多行基本字符串文本。
 * @throws Error 当文本包含三个连续单引号时抛出。
 */
function tomlMultiline(value: string, field: string): string {
  if (value.includes("'''")) throw new Error(`${field} 不能包含三个连续单引号`)
  return `'''${value}'''`
}

/**
 * 将字符串数组编码为 TOML 数组。
 *
 * @param values 待编码的字符串数组。
 * @returns TOML 字符串数组文本。
 */
function tomlStringArray(values: string[]): string {
  return `[${values.map(tomlString).join(', ')}]`
}

/**
 * 将日志库等级映射编码为 TOML 内联表。
 *
 * @param entries 库名到日志等级的映射。
 * @returns 可写入配置文件的 TOML 内联表文本。
 */
function tomlInlineTable(entries: Record<string, string>): string {
  const body = Object.entries(entries)
    .map(([key, value]) => `${key} = ${tomlString(value)}`)
    .join(', ')
  return `{ ${body} }`
}

/**
 * 序列化单个 API 服务商的 TOML 配置段。
 *
 * @param provider 待写入的服务商配置。
 * @returns 包含服务商字段、协议说明和必要配置注释的 TOML 文本。
 */
function providerBlock(provider: ApiProviderConfig): string {
  return `[[api_providers]]
# 配置内部引用名，必须唯一；models.toml 的 api_provider 写这个值
name = ${tomlString(provider.name)}
# 厂商预设名；base_url 留空时用于选择内置官方地址
kind = ${tomlString(provider.kind)}
# OpenAI 兼容 API 根地址，不要包含 /chat/completions；末尾斜杠会自动移除
base_url = ${tomlString(provider.base_url)}
# API 密钥，运行时配置为明文；不要提交 config 目录或把它贴进日志
# 豆包语音填 Access Token（不是方舟的 API Key）
api_key = ${tomlString(provider.api_key)}
# OpenAI 兼容鉴权：bearer / header / query / none
auth_type = ${tomlString(provider.auth_type)}
# header 的头名或 query 的参数名；其它模式留空
auth_name = ${tomlString(provider.auth_name)}
# 请求协议适配器：openai = OpenAI 兼容；volcengine = 豆包语音，只能用于 tts
client_type = ${tomlString(provider.client_type)}
${provider.client_type === 'volcengine' ? `# 豆包语音的 App ID，与 api_key（Access Token）成对使用
# 取自控制台：豆包语音 → 语音合成大模型 → 页面下方「服务接口认证信息」
app_id = ${tomlString(provider.app_id)}
` : ''}# 模型列表端点，用于 WebUI 连通性测试与模型拉取；OpenAI 兼容默认 /models
model_list_endpoint = ${tomlString(provider.model_list_endpoint)}
# 中转头等需要额外 HTTP 头的厂商在这里写键值；认证头仍由 auth_* 负责
default_headers = ${tomlInlineTable(provider.default_headers)}
# 中转头等需要固定查询参数的厂商在这里写键值
default_query = ${tomlInlineTable(provider.default_query)}
# 单次 HTTP 连接与流式读取超时，单位毫秒；首字阶段仍受任务级首字超时整体截断
timeout_ms = ${provider.timeout_ms}
# 同一条连接内的重试次数；需要让重试跑满时，应调大任务级首字超时
max_retries = ${provider.max_retries}
# 两次重试之间的固定等待时间，单位毫秒
retry_interval_ms = ${provider.retry_interval_ms}`
}

/**
 * 序列化单个模型定义的 TOML 配置段。
 *
 * @param model 待写入的模型配置。
 * @returns 包含模型标识、服务商引用、扩展请求体和向量维度的 TOML 文本。
 * @throws Error 当 `extra_body` 包含不支持的 TOML 值时抛出。
 */
function modelBlock(model: ModelDefinitionConfig): string {
  // 模型级温度与输出上限可留空；留空时不写键，回落到任务 generation 配置。
  const modelOverrides = model.temperature === null && model.max_tokens === null
    ? `# 模型级 temperature / max_tokens 覆盖：需要时取消注释填写，留空则用任务 generation 配置
# temperature = 0.7
# max_tokens = 2048`
    : `# 模型级温度覆盖；留空时使用任务 generation 配置，范围 0~2
temperature = ${model.temperature}
# 模型级最大输出覆盖；留空时使用任务 generation 配置
max_tokens = ${model.max_tokens}`
  return `[[models]]
# 配置内部模型名，必须唯一；上方 model_tasks 的 model_list 引用这个值
name = ${tomlString(model.name)}
# 发给厂商接口的真实模型 ID
model_identifier = ${tomlString(model.model_identifier)}
# 引用 providers.toml 中 api_providers.name
api_provider = ${tomlString(model.api_provider)}
# 原样并入请求体，按厂商接口填写
extra_body = ${tomlObject(model.extra_body, `模型 ${model.name} 的 extra_body`)}
# field = 接口字段；tag = <think>；none = 不解析
reasoning_parse_mode = ${tomlString(model.reasoning_parse_mode)}
# 视觉能力标记：只有 true 的模型才能进入 vision / 图片描述任务
visual = ${model.visual}
${modelOverrides}
# 计费参考价，单位元/百万 token；仅用于展示
price_in = ${model.price_in}
price_out = ${model.price_out}
# 向量维度，仅 embedding 模型使用；其它模型保持 0
embedding_dim = ${model.embedding_dim}`
}

/**
 * 序列化单个生成任务的 TOML 配置段。
 *
 * @param task 生成任务名称。
 * @param config 任务生成参数；主动任务还包含 `enabled` 字段。
 * @param description 写入配置文件的任务说明。
 * @returns 当前任务的 TOML 配置段文本。
 */
function generationBlock(
  task: keyof GenerationConfig,
  config: GenerationConfig[keyof GenerationConfig],
  description: string,
): string {
  const enabled = task === 'proactive'
    ? `# 是否开启主动感知与主动搭话；关闭时不安装全局键鼠钩子
enabled = ${(config as GenerationConfig['proactive']).enabled}
`
    : ''
  return `[generation.${task}]
# ${description}；temperature 越低越稳定，越高越发散，范围 0~2
${enabled}temperature = ${config.temperature}
# 最大输出 token 数；0 表示不额外限制，交给模型厂商决定
max_tokens = ${config.max_tokens}`
}

/**
 * 将服务商配置序列化为 `providers.toml` 文件内容。
 *
 * @param cfg 完整运行时配置；仅使用其中的服务商数组。
 * @returns 当前配置版本、服务商定义及说明注释组成的 TOML 文本。
 */
function serializeProviders(cfg: YueliConfig): string {
  return `# API 厂商与连接策略。具体模型不要写在这里。
# 一个厂商可供多个模型复用；api_key 当前为明文，请勿提交 config 目录。
# 若任务需要在服务商不可用时自动切换，请配置多条连接，再在 models.toml
# 中为每条连接定义模型，并将对应模型一并写入 model_tasks 的 model_list。

[inner]
# 配置结构版本；手工修改为未知版本会拒绝启动，避免错误解释字段
version = ${tomlString(CONFIG_VERSION)}

${cfg.api_providers.map(providerBlock).join('\n\n')}
`
}

const TASK_DESCRIPTIONS: Record<ModelTask, string> = {
  chat: '用户聊天',
  proactive: '主动搭话；留空时继承用户聊天候选',
  summary: '长期记忆摘要；留空时继承用户聊天候选',
  schedule: '每日生活计划；留空时继承用户聊天候选',
  vision: '前台窗口图片理解；模型和接口都必须接受图片消息',
  expression: '挑选表达方式；分类型小任务，留空时继承用户聊天候选',
  planner: '行动决策；留空时继承用户聊天候选。首字延迟主要由它决定',
  replyer: '回复生成；留空时继承用户聊天候选',
  scene: '情景分析；群聊画像与私聊追问判断都用它，留空时继承用户聊天候选',
  memory: '记忆抽取；后台任务不在回复关键路径，做结构化抽取，配便宜快的模型。留空时继承用户聊天候选',
  tts: '语音合成',
  embedding: '向量记忆召回',
}

/**
 * 序列化单个任务的候选模型列表和轮询策略。
 *
 * @param task 任务名称。
 * @param routing 该任务的模型候选和超时配置。
 * @returns 当前任务的 TOML 配置段文本；候选顺序保留主备优先级。
 */
function taskBlock(task: ModelTask, routing: TaskRoutingConfig): string {
  return `[model_tasks.${task}]
# ${TASK_DESCRIPTIONS[task]}使用的模型定义名，按优先级从前往后写
model_list = ${tomlStringArray(routing.model_list)}
# 挑选顺序：sequential = 主力优先；random = 随机；balance = 健康候选逐轮分摊
# 无论哪种，刚失败过的厂商都会在冷却期内被排到最后
selection_strategy = ${tomlString(routing.selection_strategy)}
# 流式任务等待首字的上限，超时后切换候选
first_token_timeout_ms = ${routing.first_token_timeout_ms}
# 首字达到该耗时就记慢事件；0 表示关闭
slow_threshold_ms = ${routing.slow_threshold_ms}`
}

/**
 * 将模型、任务路由和生成参数序列化为 `models.toml` 文件内容。
 *
 * @param cfg 完整运行时配置；使用其中的模型、路由和生成参数。
 * @returns 当前配置版本、任务路由、生成参数及模型定义组成的 TOML 文本。
 * @throws Error 当模型扩展请求体包含不支持的 TOML 值时抛出。
 */
function serializeModels(cfg: YueliConfig): string {
  const generationDescriptions: Record<keyof GenerationConfig, string> = {
    chat: '用户主动聊天的回复参数',
    proactive: '桌宠主动搭话的回复参数',
    summary: '长期记忆摘要的生成参数',
    schedule: '每日生活计划的生成参数',
    expression: '挑选表达方式的生成参数',
    vision: '前台窗口视觉描述的生成参数',
    planner: '行动决策的生成参数；决策不产出正文，想让动作更稳可单独调低',
    replyer: '回复生成的生成参数；写她实际说出口的那句话',
    scene: '情景分析的生成参数；要稳定概括而不是发挥',
    memory: '记忆抽取的生成参数；要稳定的结构化输出，温度取最低一档',
  }
  const generation = (Object.keys(generationDescriptions) as Array<keyof GenerationConfig>)
    .map((task) => generationBlock(task, cfg.generation[task], generationDescriptions[task]))
    .join('\n\n')
  const tasks = MODEL_TASKS.map((task) => taskBlock(task, cfg.model_tasks[task])).join('\n\n')
  return `# 具体模型、任务的候选模型列表与各任务生成参数。
# 模型只引用 providers.toml 的连接名，不在这里重复地址和密钥。

[inner]
# 配置结构版本
version = ${tomlString(CONFIG_VERSION)}

${tasks}

${generation}

${cfg.models.map(modelBlock).join('\n\n')}
`
}

/**
 * 将 Bot 身份、人格、群聊、日程和会话配置序列化为 `bot.toml`。
 *
 * @param cfg 完整运行时配置；使用其中的 Bot、群聊、日程、人格和会话字段。
 * @returns 当前配置版本及 Bot 相关配置组成的 TOML 文本。
 * @throws Error 当多行人格文本包含 TOML 分隔符，或字符串数组无法编码时抛出。
 */
function serializeBot(cfg: YueliConfig): string {
  // 先单独编码数组字段，保证模板主体只负责组织配置段，不重复处理转义规则。
  const tones = cfg.personality.tone_variants.map((tone) => `  ${tomlString(tone)},`).join('\n')
  return `# Bot 身份、用户关系、人格与对话记忆策略。
# 功能开关和模型连接信息分别放在 features.toml 与 providers/models.toml。

[inner]
# 配置结构版本
version = ${tomlString(CONFIG_VERSION)}

[bot]
# Bot 的显示名，也会作为系统提示词中的身份名
name = ${tomlString(cfg.bot.name)}
# 群聊里也会回应的其它称呼
aliases = ${tomlStringArray(cfg.bot.aliases)}
# Bot 对用户的称呼偏好；留空表示不主动使用特定称呼
user_nickname = ${tomlString(cfg.bot.user_nickname)}
# Bot 和你的关系，例如“哥哥”“姐姐”“朋友”；留空则不预设关系
relationship = ${tomlString(cfg.bot.relationship)}

[group_chat]
# true 时协议 @ 提及不受睡眠与群聊回复窗口限制
at_mention_must_reply = ${cfg.group_chat.at_mention_must_reply}
# 名字、别名或非必回 @ 命中后的回复概率，范围 0~1
name_mention_probability = ${cfg.group_chat.name_mention_probability}
# 存在感衰减强度，范围 0~20；越大，群里说得越多时越倾向于让别人先说
presence_decay_strength = ${cfg.group_chat.presence_decay_strength}
# 群聊里人格增量的折算系数，0~1；群里一句一答的消耗远小于面对面长聊
persona_weight = ${cfg.group_chat.persona_weight}
# 在这段时间窗口内统计 Bot 已经回复了多少次
reply_window_minutes = ${cfg.group_chat.reply_window_minutes}
# 非必回消息在时间窗口内允许的最大回复次数
max_replies_in_window = ${cfg.group_chat.max_replies_in_window}
# 允许对群消息贴表情回应（在别人消息上点一个表情，不发新消息）
reactions_enabled = ${cfg.group_chat.reactions_enabled}
# 允许使用 QQ 戳一戳；它比贴表情吵得多，默认关闭
pokes_enabled = ${cfg.group_chat.pokes_enabled}
# 允许她主动起话头（不接任何人的话）；没什么非说不可的仍然该选沉默
self_started_topics = ${cfg.group_chat.self_started_topics}
# 观察任务刷新场景画像前跳过的消息条数；0 表示不刷新
scene_refresh_messages = ${cfg.group_chat.scene_refresh_messages}

[schedule]
# 是否允许活动决策选择 sleep；关闭后仍可选择会回应的 rest
sleep_enabled = ${cfg.schedule.sleep_enabled}
# 每日方向模型不可用或输出不合法时使用的主题
fallback_theme = ${tomlString(cfg.schedule.fallback_theme)}
# 生成失败后再次尝试前等待的分钟数
generation_retry_interval_minutes = ${cfg.schedule.generation_retry_interval_minutes}

[personality]
# 生日，格式 YYYY-MM-DD；留空时不派生年龄与生日提示
birthday = ${tomlString(cfg.personality.birthday)}
# 稳定身份、经历、外表与性格；只从这份 Bot 配置进入提示词
personality = ${tomlMultiline(cfg.personality.personality, 'personality.personality')}
# 句长、语气、排版和收尾习惯
reply_style = ${tomlMultiline(cfg.personality.reply_style, 'personality.reply_style')}
# 新会话抽取临时语调的概率，范围 0~1；0 表示始终不抽取
tone_probability = ${cfg.personality.tone_probability}
# 候选的会话级语调，只在新会话开始时至多抽取一条
tone_variants = [
${tones}
]

[conversation]
# 每轮送进模型的最近消息条数，不含系统提示词
working_memory_messages = ${cfg.conversation.working_memory_messages}
# 未摘要消息达到此数量后触发一次长期记忆摘要
summarize_trigger_messages = ${cfg.conversation.summarize_trigger_messages}
# 每次摘要消化的最老消息条数，必须小于触发数量
summarize_batch_messages = ${cfg.conversation.summarize_batch_messages}
# 超过此空闲时长视为新会话，并重新抽取临时语调，单位分钟
session_gap_minutes = ${cfg.conversation.session_gap_minutes}
# 每轮最多召回的事实记忆数量；0 表示不召回事实
fact_recall_limit = ${cfg.conversation.fact_recall_limit}
# 按当前输入相关性召回的情节数量
recalled_episode_limit = ${cfg.conversation.recalled_episode_limit}
# 无论相关性如何都补充的最近情节数量
recent_episode_limit = ${cfg.conversation.recent_episode_limit}
# 去重后最终写入系统提示词的情节总上限
episode_context_limit = ${cfg.conversation.episode_context_limit}
# 未抽取消息达到此数量后触发一次事实抽取；与摘要各自维护游标互不影响
fact_extract_trigger_messages = ${cfg.conversation.fact_extract_trigger_messages}
# 每次事实抽取消化的最老消息条数
fact_extract_batch_messages = ${cfg.conversation.fact_extract_batch_messages}
# 私聊（含桌面端）听到的事实能否出现在群聊提示词里；默认只在被听见的场合可见
private_facts_in_group = ${cfg.conversation.private_facts_in_group}

[conversation_agent]
# 对话 Agent 运行模式：off 关闭 / shadow 只记录不改行为 / selected_streams 仅指定会话 / enabled 全量
mode = ${tomlString(cfg.conversation_agent.mode)}
# mode = selected_streams 时生效；写会话标识，其余会话保持旧管线
selected_streams = ${tomlStringArray(cfg.conversation_agent.selected_streams)}
# 触发口径：signal 有明确信号才回 / frequency 按频率值轮到就回 / reply_necessity 按回复必要性打分
trigger_mode = ${tomlString(cfg.conversation_agent.trigger_mode)}
# trigger_mode = frequency 时的阈值，范围 0~1（不含 0）
frequency_talk_value = ${cfg.conversation_agent.frequency_talk_value}
# trigger_mode = reply_necessity 时的必要性分数阈值，0~100
reply_necessity_threshold = ${cfg.conversation_agent.reply_necessity_threshold}
# 一回合允许的检索（recall/inspect）次数上限，0~4
max_cognitive_rounds = ${cfg.conversation_agent.max_cognitive_rounds}
# 决策与表达分离：决策层只选动作，回复生成层写正文
split_replyer = ${cfg.conversation_agent.split_replyer}
# 工具调用模式：动作空间由工具声明承载，而不是 XML 动作头
tool_calling = ${cfg.conversation_agent.tool_calling}

[typing]
# 一句话的目标字符数，超过就按气泡拆开发送
bubble_target_chars = ${cfg.typing.bubble_target_chars}
# 一句话最多拆成几个气泡
max_bubbles_per_say = ${cfg.typing.max_bubbles_per_say}
# 是否按打字节奏延迟发送；关闭则整句立即发出
delay_enabled = ${cfg.typing.delay_enabled}
# 每个中文字符的模拟输入秒数
chinese_char_seconds = ${cfg.typing.chinese_char_seconds}
# 每个英文字符的模拟输入秒数
latin_char_seconds = ${cfg.typing.latin_char_seconds}
# 两个气泡之间的间隔秒数
send_gap_seconds = ${cfg.typing.send_gap_seconds}
# 单条消息延迟上限秒数；再长的话也按时发出
max_delay_seconds = ${cfg.typing.max_delay_seconds}
# 挑选表情包的固定秒数
emoji_pick_seconds = ${cfg.typing.emoji_pick_seconds}

[typing.follow_up]
# 对方停止输入后是否补发未说完的话
enabled = ${cfg.typing.follow_up.enabled}
# 对方静默多少分钟后不再补发
peer_silence_minutes = ${cfg.typing.follow_up.peer_silence_minutes}

[typing.nudge]
# 对方输入中却迟迟不发时，是否轻轻戳一下催一催
enabled = ${cfg.typing.nudge.enabled}
# 对方输入中静默多少分钟才考虑戳
peer_silence_minutes = ${cfg.typing.nudge.peer_silence_minutes}
# 一次静默期内最多戳几次；戳多了比不戳难受
max_per_silence = ${cfg.typing.nudge.max_per_silence}

[emoji]
# 可发送表情的最大条数；0 表示不限。超过后按「最少用、最久没用」淘汰
max_count = ${cfg.emoji.max_count}
# 库满后是否自动淘汰最冷的条目；关闭时只告警不删除
auto_evict = ${cfg.emoji.auto_evict}
# 两次库容量检查之间的最小间隔，单位分钟
check_interval_minutes = ${cfg.emoji.check_interval_minutes}
# 收集时的单文件大小上限，单位 MB；0 表示不限
max_file_size_mb = ${cfg.emoji.max_file_size_mb}
# 入库前是否调用视觉模型审查内容；开启但视觉模型不可用时会拒绝入库
content_filtration = ${cfg.emoji.content_filtration}
# 是否从聊天里自动收集表情包；关闭后入站图片只识别不入库
collect_enabled = ${cfg.emoji.collect_enabled}

[emoji.cleanup]
# 是否定期清理目录里库里没有记录的孤儿文件
enabled = ${cfg.emoji.cleanup.enabled}
# 两次清理检查之间的最小间隔，单位小时
check_interval_hours = ${cfg.emoji.cleanup.check_interval_hours}
# 孤儿文件至少保留多少天；0 表示下次检查时立即清理
orphan_retention_days = ${cfg.emoji.cleanup.orphan_retention_days}

[desktop_pet]
# 【实验性功能，默认关闭，开启后可能遇到未知问题】
# 是否启用桌宠窗口；关闭后不创建窗口与桌面感知，只保留托盘与后端，改动重启应用后生效
enabled = ${cfg.desktop_pet.enabled}
`
}

/**
 * 将功能开关、日志、感知和代理参数序列化为 `features.toml`。
 *
 * @param cfg 完整运行时配置；使用其中的 TTS、视觉、感知、向量、日志和高级选项。
 * @returns 当前配置版本及功能参数组成的 TOML 文本。
 * @throws Error 当日志映射或其他字符串值无法编码为 TOML 时抛出。
 */
function serializeFeatures(cfg: YueliConfig): string {
  // 该文件只保存运行能力与基础设施参数，服务商密钥和模型标识由另外两个文件维护。
  return `# 功能开关与运行参数。服务地址、密钥和模型分别在 providers/models 中维护。

[inner]
# 配置结构版本
version = ${tomlString(CONFIG_VERSION)}

[tts]
# 是否启用语音合成；关闭时保持纯文字回复
enabled = ${tomlValue(cfg.tts.enabled)}
# 厂商提供的音色 ID；不是显示名称。豆包语音这里填 voice_type
voice = ${tomlValue(cfg.tts.voice)}
# 返回音频格式：mp3 / wav / opus
format = ${tomlValue(cfg.tts.format)}
# 语速倍率，范围 0.25~4.0；陪伴场景建议略低于 1
speed = ${tomlValue(cfg.tts.speed)}
# 仅 client_type = "volcengine" 时生效：豆包语音的集群名
cluster = ${tomlValue(cfg.tts.cluster)}

[vision]
# 用户询问屏幕内容时截取一帧发送给视觉模型；未询问时不截取。默认关闭
enabled = ${tomlValue(cfg.vision.enabled)}
# 允许理解 QQ 聊天里收到的图片；候选模型必须标记 visual = true
chat_image_enabled = ${tomlValue(cfg.vision.chat_image_enabled)}
# 检测到疑似全屏窗口时是否保持静默，避免直播或录屏意外播报
fullscreen_silent = ${tomlValue(cfg.vision.fullscreen_silent)}
# 截什么："window" 只截前台那一个窗口；"screen" 截整个主屏。
# screen 会让视觉服务获取整个主屏画面，可能同时包含其他窗口、后台聊天和浏览器页面；
# 向云端模型发送前应确认采集范围符合隐私要求。
capture_mode = ${tomlValue(cfg.vision.capture_mode)}

[perception]
# 允许 Bot 在哪些出口提及前台程序、持续时间，以及开启 [vision] 后得到的屏幕内容。
# 可填 "desktop"（桌宠窗口）与 "direct"（QQ 私聊）；留空表示不在任何出口提及。
# 群聊不是可选项，填进去会在加载期直接报错。
surfaces = ${tomlStringArray(cfg.perception.surfaces)}

[vector]
# 是否启用向量混合召回；还需要安装项目的 vector 可选依赖
enabled = ${tomlValue(cfg.vector.enabled)}

[developer]
# owner 私聊专用开发者命令通道；默认关闭，只有手工确认后才应开启
enabled = ${tomlValue(cfg.developer.enabled)}

# 反馈纠错（N4）：事实进过提示词后被用户纠正时，按事实账本取代机制改库。
# 整条链路默认关闭，开启是显式动作。
[memory_feedback]
# 总开关；关闭时整条链路零写入
enabled = ${tomlValue(cfg.memory_feedback.enabled)}
# 从记忆进提示词起算的反馈观察窗口（小时）
window_hours = ${tomlValue(cfg.memory_feedback.window_hours)}
# 纠错轮询间隔（分钟）
check_interval_minutes = ${tomlValue(cfg.memory_feedback.check_interval_minutes)}
# 每轮最多处理的待观察项
batch_size = ${tomlValue(cfg.memory_feedback.batch_size)}
# 自动应用取代的最低置信度
auto_apply_threshold = ${tomlValue(cfg.memory_feedback.auto_apply_threshold)}
# 每个待观察项最多读取的窗口内用户消息数
max_feedback_messages = ${tomlValue(cfg.memory_feedback.max_feedback_messages)}
# 关键词预筛开关；关闭会显著增加模型调用
prefilter_enabled = ${tomlValue(cfg.memory_feedback.prefilter_enabled)}
# 是否给受影响事实写「已被纠正」标记
mark_enabled = ${tomlValue(cfg.memory_feedback.mark_enabled)}
# 是否把带标记的事实硬过滤出召回
hard_filter_enabled = ${tomlValue(cfg.memory_feedback.hard_filter_enabled)}
# 纠错后是否把相关人物画像置脏
profile_refresh_enabled = ${tomlValue(cfg.memory_feedback.profile_refresh_enabled)}
# 画像脏时读取是否强制刷新而非复用旧快照
profile_force_refresh_on_read = ${tomlValue(cfg.memory_feedback.profile_force_refresh_on_read)}
# 纠错后是否把受影响情节排进重建
episode_rebuild_enabled = ${tomlValue(cfg.memory_feedback.episode_rebuild_enabled)}
# 情节待重建期间是否屏蔽它的召回
episode_query_block_enabled = ${tomlValue(cfg.memory_feedback.episode_query_block_enabled)}
# 二阶段一致性协调任务的轮询间隔（分钟）
reconcile_interval_minutes = ${tomlValue(cfg.memory_feedback.reconcile_interval_minutes)}
# 协调任务每轮的批大小
reconcile_batch_size = ${tomlValue(cfg.memory_feedback.reconcile_batch_size)}

[log]
# 全局日志等级：DEBUG / INFO / WARNING / ERROR / CRITICAL
level = ${tomlValue(cfg.log.level)}
# 终端和文件各自的等级，留空跟随 level
console_level = ${tomlValue(cfg.log.console_level)}
file_level = ${tomlValue(cfg.log.file_level)}
# 控制台等级列：lite 只体现在时间戳颜色上，compact 显示单字母，full 显示全称
level_style = ${tomlValue(cfg.log.level_style)}
# 着色范围：none 不着色，title 只染时间戳与模块名，full 连正文一起染
color_scope = ${tomlValue(cfg.log.color_scope)}
# 时间戳格式，strftime 语法
date_format = ${tomlValue(cfg.log.date_format)}
# 是否把日志写进 <数据目录>/logs/app_*.log.jsonl
to_file = ${tomlValue(cfg.log.to_file)}
# 单个日志文件上限，单位字节，超过就换新文件
file_max_bytes = ${tomlValue(cfg.log.file_max_bytes)}
# 最多保留几个日志文件，超出的从最旧的删起
max_files = ${tomlValue(cfg.log.max_files)}
# 超过这些天的日志文件直接清掉，0 表示只按数量
cleanup_days = ${tomlValue(cfg.log.cleanup_days)}
# 按库名压噪音，没列出的库跟随 level
library_levels = ${tomlInlineTable(cfg.log.library_levels)}
# 完全不要的库，一行都不输出
suppress_libraries = ${tomlStringArray(cfg.log.suppress_libraries)}
# 模型调用失败时，把实际发出的请求体存进 logs/llm_request/（密钥已隐去）
request_snapshots = ${tomlValue(cfg.log.request_snapshots)}
# 最多保留几份快照
max_snapshot_files = ${tomlValue(cfg.log.max_snapshot_files)}
# 每次模型调用（成功也算）按任务分目录存进 logs/prompt/<任务>/，密钥已隐去
prompt_records = ${tomlValue(cfg.log.prompt_records)}
# 每个任务子目录保留的记录份数；按任务分别计数，高频任务不挤掉低频任务
max_prompt_records_per_task = ${tomlValue(cfg.log.max_prompt_records_per_task)}
# 管线事件最多保留多少条
event_retention_count = ${tomlValue(cfg.log.event_retention_count)}
# 管线事件最多保留多少小时
event_retention_hours = ${tomlValue(cfg.log.event_retention_hours)}

[advanced]
# 全局 HTTP(S) 代理，例如 http://127.0.0.1:7890；留空表示直连
https_proxy = ${tomlValue(cfg.advanced.https_proxy)}
`
}

/**
 * 校验设置页提交的配置结构、任务路由和功能开关之间的一致性。
 *
 * @param cfg 待校验的完整运行时配置；服务商的 `auth_name` 可能被原地规范化。
 * @returns {void} 无返回值；配置通过全部结构、范围和引用校验时正常返回。
 * @throws Error 当身份字段、数值范围、引用关系、任务超时、功能候选或向量维度不满足约束时抛出。
 * @remarks 该方法只校验结构自洽性，不要求所有可选功能都已配置完成；启动向导的最小可用性由
 * {@link configIsComplete} 判断，目录迁移则使用读取阶段的兼容规则。
 */
export function assertConfigConsistent(cfg: YueliConfig): void {
  const botName = cfg.bot.name.trim()
  const aliases = cfg.bot.aliases.map((alias) => alias.trim())
  if (!botName) throw new Error('Bot 名字不能为空')
  if (aliases.some((alias) => !alias)) throw new Error('Bot 别名不能包含空字符串')
  if (aliases.includes(botName)) throw new Error('Bot 别名不要重复 Bot 名字')
  if (new Set(aliases).size !== aliases.length) throw new Error('Bot 别名不能重复')
  assertBirthday(cfg.personality.birthday, 'personality.birthday')
  if (cfg.personality.tone_probability < 0 || cfg.personality.tone_probability > 1) {
    throw new Error('临时说话风格概率必须在 0 到 1 之间')
  }
  const personalityTextLists = [
    ['临时说话风格', cfg.personality.tone_variants],
  ] as const
  for (const [label, values] of personalityTextLists) {
    if (values.some((value) => !value.trim())) throw new Error(`${label}不能包含空字符串`)
  }
  if (
    cfg.group_chat.name_mention_probability < 0
    || cfg.group_chat.name_mention_probability > 1
  ) {
    throw new Error('名字或别名触发回复概率必须在 0 到 1 之间')
  }
  if (
    cfg.group_chat.presence_decay_strength < 0
    || cfg.group_chat.presence_decay_strength > 20
  ) {
    throw new Error('群聊存在感衰减强度必须在 0 到 20 之间')
  }
  if (cfg.group_chat.persona_weight < 0 || cfg.group_chat.persona_weight > 1) {
    throw new Error('群聊人格增量折算系数必须在 0 到 1 之间')
  }
  if (
    !Number.isInteger(cfg.group_chat.reply_window_minutes)
    || cfg.group_chat.reply_window_minutes < 1
  ) {
    throw new Error('群聊回复窗口分钟数必须是正整数')
  }
  if (
    !Number.isInteger(cfg.group_chat.max_replies_in_window)
    || cfg.group_chat.max_replies_in_window < 0
  ) {
    throw new Error('群聊窗口内最大回复次数必须是非负整数')
  }
  assertSchedule(cfg.schedule, '日程配置')
  if (!Number.isInteger(cfg.log.event_retention_count) || cfg.log.event_retention_count < 1) {
    throw new Error('事件保留条数必须是正整数')
  }
  if (!Number.isInteger(cfg.log.event_retention_hours) || cfg.log.event_retention_hours < 0) {
    throw new Error('事件保留小时数必须是非负整数')
  }
  if (!(cfg.memory_feedback.window_hours > 0)) {
    throw new Error('反馈观察窗口小时数必须大于 0')
  }
  if (
    cfg.memory_feedback.auto_apply_threshold < 0
    || cfg.memory_feedback.auto_apply_threshold > 1
  ) {
    throw new Error('自动应用取代的最低置信度必须在 0 到 1 之间')
  }
  for (const [label, value] of [
    ['纠错轮询间隔分钟数', cfg.memory_feedback.check_interval_minutes],
    ['每轮最多处理的待观察项数', cfg.memory_feedback.batch_size],
    ['每个待观察项最多读取的用户消息数', cfg.memory_feedback.max_feedback_messages],
    ['一致性协调轮询间隔分钟数', cfg.memory_feedback.reconcile_interval_minutes],
    ['协调任务每轮的批大小', cfg.memory_feedback.reconcile_batch_size],
  ] as const) {
    if (!Number.isInteger(value) || value < 1) {
      throw new Error(`${label}必须是正整数`)
    }
  }
  if (!Array.isArray(cfg.perception.surfaces)) {
    throw new Error('perception.surfaces 必须是数组')
  }
  for (const surface of cfg.perception.surfaces as string[]) {
    if (surface === 'group') {
      throw new Error('群聊不能启用屏幕情境：群消息会被多人看见，屏幕内容一旦发出无法撤回')
    }
    if (surface !== 'desktop' && surface !== 'direct') {
      throw new Error(`perception.surfaces 只能填写 desktop 或 direct：${surface}`)
    }
  }
  for (const [task, generation] of Object.entries(cfg.generation)) {
    if (
      !Number.isFinite(generation.temperature)
      || generation.temperature < 0
      || generation.temperature > 2
    ) {
      throw new Error(`generation.${task}.temperature 必须在 0 到 2 之间`)
    }
    if (
      !Number.isInteger(generation.max_tokens)
      || generation.max_tokens < 0
      || generation.max_tokens > 1_000_000
    ) {
      throw new Error(`generation.${task}.max_tokens 必须是 0 到 1000000 的整数`)
    }
  }
  const providerNames = cfg.api_providers.map((provider) => provider.name.trim())
  if (providerNames.some((name) => !name)) throw new Error('每个服务商都要有名称')
  if (new Set(providerNames).size !== providerNames.length) {
    throw new Error('服务商名称不能重复')
  }
  cfg.api_providers.forEach((provider) => {
    validateProviderAuth(provider, `服务商 ${provider.name}`)
  })
  const modelNames = cfg.models.map((model) => model.name.trim())
  if (modelNames.some((name) => !name)) throw new Error('每个模型都要有名称')
  if (new Set(modelNames).size !== modelNames.length) throw new Error('模型名称不能重复')

  for (const model of cfg.models) {
    tomlObject(model.extra_body, `模型 ${model.name} 的 extra_body`)
    if (!['field', 'tag', 'none'].includes(model.reasoning_parse_mode)) {
      throw new Error(`模型 ${model.name} 的 reasoning_parse_mode 无效`)
    }
    if (!cfg.api_providers.some((provider) => provider.name === model.api_provider)) {
      throw new Error(`模型 ${model.name} 挂在不存在的服务商 ${model.api_provider} 上`)
    }
  }
  for (const task of MODEL_TASKS) {
    const routing = cfg.model_tasks[task]
    if (!Number.isInteger(routing.first_token_timeout_ms) || routing.first_token_timeout_ms < 1_000) {
      throw new Error(`${TASK_DESCRIPTIONS[task]}的 first_token_timeout_ms 必须至少为 1000`)
    }
    if (!Number.isInteger(routing.slow_threshold_ms) || routing.slow_threshold_ms < 0) {
      throw new Error(`${TASK_DESCRIPTIONS[task]}的 slow_threshold_ms 必须是非负整数`)
    }
    if (routing.slow_threshold_ms !== 0
        && routing.slow_threshold_ms >= routing.first_token_timeout_ms) {
      throw new Error(
        `${TASK_DESCRIPTIONS[task]}的 slow_threshold_ms 必须小于 first_token_timeout_ms，或设为 0`,
      )
    }
    if (new Set(routing.model_list).size !== routing.model_list.length) {
      throw new Error(`${TASK_DESCRIPTIONS[task]}的候选里有重复模型`)
    }
    for (const name of routing.model_list) {
      const model = cfg.models.find((candidate) => candidate.name === name)
      if (!model) throw new Error(`${TASK_DESCRIPTIONS[task]}引用了不存在的模型：${name}`)
      const provider = cfg.api_providers.find((item) => item.name === model.api_provider)!
      // 私有语音协议仅允许绑定语音任务；提前拒绝可避免请求阶段才出现协议不匹配。
      if (task !== 'tts' && provider.client_type !== 'openai') {
        throw new Error(
          `${TASK_DESCRIPTIONS[task]}不能用豆包语音协议的服务商（${provider.name}）`,
        )
      }
      if (provider.client_type === 'openai' && !model.model_identifier.trim()) {
        throw new Error(`模型 ${name} 还没填模型 ID`)
      }
    }
  }

  // 已启用功能必须存在候选模型，否则配置表面启用但运行时永远不会执行。
  for (const [enabled, task] of [
    [cfg.tts.enabled, 'tts'],
    [cfg.vision.enabled, 'vision'],
    [cfg.vector.enabled, 'embedding'],
    [cfg.emoji.content_filtration, 'vision'],
  ] as const) {
    if (enabled && cfg.model_tasks[task].model_list.length === 0) {
      throw new Error(`启用了${TASK_DESCRIPTIONS[task]}，就要给它至少一个候选模型`)
    }
  }
  if (cfg.tts.enabled && !cfg.tts.voice.trim()) throw new Error('启用语音合成就要填音色')

  // 候选向量模型必须共享维度，否则备用模型返回的向量无法与既有索引计算相似度。
  const dims = new Set(cfg.model_tasks.embedding.model_list.map(
    (name) => cfg.models.find((model) => model.name === name)!.embedding_dim,
  ))
  if (dims.size > 1) throw new Error('向量记忆的候选模型必须是同一个向量维度')
}

/**
 * 将运行时配置序列化并写入四个职责单一的 TOML 配置文件。
 *
 * @param directory 目标配置目录；不存在时递归创建。
 * @param cfg 已通过结构校验的运行时配置。
 * @returns {void} 无返回值；四个配置文件全部写入后完成。
 * @throws Error 当目标路径不是目录、目录创建失败或任一文件写入失败时抛出。
 * @remarks 方法会覆盖目标目录中的四个当前配置文件，但不修改目录中的其他文件。
 */
export function writeConfigDirectory(directory: string, cfg: YueliConfig): void {
  if (existsSync(directory) && !statSync(directory).isDirectory()) {
    throw new Error(`${directory} 存在，但不是配置目录`)
  }
  mkdirSync(directory, { recursive: true })
  const documents: Record<(typeof CONFIG_FILES)[number], string> = {
    'providers.toml': serializeProviders(cfg),
    'models.toml': serializeModels(cfg),
    'bot.toml': serializeBot(cfg),
    'features.toml': serializeFeatures(cfg),
  }
  for (const name of CONFIG_FILES) {
    writeFileSync(join(directory, name), documents[name], 'utf-8')
  }
}

/** 当前适配器声明文件名与其中的字段名；与 src/core/config/adapter_selection.py 同名同义。 */
const ADAPTER_SELECTION_FILE = 'adapter.toml'
const ADAPTER_SELECTION_FIELD = 'plugin'
/** 新装时默认启用的适配器插件目录名，仅在创建声明文件时使用一次。 */
const DEFAULT_ADAPTER_PLUGIN = 'yueli-snowluma-adapter'

/**
 * 读取当前启用的适配器插件目录名，文件不存在时按默认值创建声明。
 *
 * 两个协议端后端互斥，同时只能开一个。这个事实必须只有一处来源：以前它写死在
 * 监护器常量里，主体侧读不到，设置页只能按固定文件名去读一份可能没人用的配置。
 *
 * @param configDir 主体配置目录；不存在时递归创建。
 * @returns ``adapters/`` 下的插件目录名。
 * @throws Error 声明文件不是合法 TOML，或其中的插件目录名为空。
 */
export function ensureAdapterSelection(configDir: string): string {
  const path = join(configDir, ADAPTER_SELECTION_FILE)
  if (!existsSync(path)) {
    mkdirSync(configDir, { recursive: true })
    writeFileSync(path, [
      '# 当前启用的 QQ 适配器：adapters/ 下的插件目录名。',
      '# 两个协议端后端互斥，同时只能开一个；桌宠与主体都读这一处声明。',
      `${ADAPTER_SELECTION_FIELD} = ${tomlString(DEFAULT_ADAPTER_PLUGIN)}`,
      '',
    ].join('\n'), 'utf-8')
    console.log(`[config] 已创建适配器声明：${path}`)
    return DEFAULT_ADAPTER_PLUGIN
  }
  const document = TOML.parse(readFileSync(path, 'utf-8')) as Record<string, unknown>
  const plugin = document[ADAPTER_SELECTION_FIELD]
  if (typeof plugin !== 'string' || !plugin.trim()) {
    throw new Error(`${path} 缺少非空的 ${ADAPTER_SELECTION_FIELD}，无法确定启用哪个适配器`)
  }
  return plugin.trim()
}

/**
 * 从适配器清单读出该适配器要读的连接段名。
 *
 * @param adapterDir 适配器插件目录。
 * @returns 清单中的 `config_section`。
 * @throws Error 清单缺失、不是合法 JSON，或没有非空 `config_section` 时抛出。
 * 段名猜错写出的配置该适配器根本读不了，退回一个默认段只会让错误挪到启动时。
 */
function readAdapterConfigSection(adapterDir: string): string {
  const manifestPath = join(adapterDir, '_manifest.json')
  if (!existsSync(manifestPath)) {
    throw new Error(`${manifestPath} 不存在，无法确定适配器的连接段名`)
  }
  const manifest: unknown = JSON.parse(readFileSync(manifestPath, 'utf-8'))
  const section = (manifest as { config_section?: unknown }).config_section
  if (typeof section !== 'string' || !section.trim()) {
    throw new Error(`${manifestPath} 缺少非空的 config_section`)
  }
  return section.trim()
}

/**
 * 在首次启动时创建停用状态的 QQ 适配器配置模板。
 *
 * @param adapterDir 适配器插件目录；必须已存在且含 `_manifest.json`。
 * @returns 该目录下 `config.toml` 的完整路径。
 * @throws Error 当目录或目标路径类型不正确、清单缺失或不含连接段名、模板写入失败时抛出。
 * @remarks 连接配置与插件同目录，由插件按自身位置读取，与主体的 config/ 无关；
 * 已存在的文件只校验其为普通文件，不覆盖原有内容；并发创建时保留先写入者的文件。
 */
export function ensureAdapterConfig(adapterDir: string): string {
  // 适配器目录随应用一起分发，缺失说明安装不完整。这里不 mkdir 补一个空目录：
  // 补出来的目录没有 plugin.py 也没有清单，适配器进程照样起不来，只是把同一个
  // 故障推迟到启动时，还少了一条指向真正原因的报错。
  if (!existsSync(adapterDir) || !statSync(adapterDir).isDirectory()) {
    throw new Error(`${adapterDir} 不是适配器插件目录，无法创建连接配置`)
  }
  const path = join(adapterDir, 'config.toml')
  if (existsSync(path)) {
    if (!statSync(path).isFile()) throw new Error(`${path} 存在，但不是 QQ 配置文件`)
    return path
  }
  let created = false
  try {
    const template = adapterConfigTemplate(readAdapterConfigSection(adapterDir))
    writeFileSync(path, template, { encoding: 'utf-8', flag: 'wx' })
    created = true
  } catch (error) {
    if (
      !(error instanceof Error)
      || !('code' in error)
      || error.code !== 'EEXIST'
    ) {
      throw error
    }
  }
  if (created) console.log(`[config] 已创建 QQ 适配器停用配置模板：${path}`)
  return path
}

/**
 * 从旧版 `.env` 文件提取可迁移的模型连接字段，并把已废弃的 `LLM_THINKING`
 * 自动转换为模型条目 `extra_body` 中的厂商参数。
 *
 * @param envPath `.env` 文件路径。
 * @returns 当文件不存在或不包含模型连接字段时返回 `null`；否则返回可合并到配置表的部分配置。
 * @throws Error 当文件无法解析或预填数据结构无法构建时抛出。
 * @remarks 方法只读取并转换已存在的字段，不写回 `.env`，也不要求迁移结果已经完整可启动；
 *   思考开关按旧版运行链的实际生效范围转换，未生效的字段不会写入 `extra_body`。
 */
export function tryPrefillFromLegacyEnv(envPath: string): Partial<YueliConfig> | null {
  if (!existsSync(envPath)) {
    console.log(`[config] 未发现旧版 .env（${envPath}），跳过迁移`)
    return null
  }
  try {
    const parsed = parseDotenv(readFileSync(envPath))
    if (!parsed.LLM_API_KEY && !parsed.LLM_MODEL) {
      console.log(`[config] 旧版 .env（${envPath}）不含模型连接字段，无需迁移`)
      return null
    }
    const baseUrl = parsed.LLM_BASE_URL || ''
    // 旧 `.env` 经常只填 base_url 而不填 provider；从已知域名反推 kind，避免
    // 把 DeepSeek 等连接误归入默认的 ark 预设。
    const kind = (parsed.LLM_PROVIDER || '').trim().toLowerCase()
      || inferLegacyProviderKind(baseUrl)
      || DEFAULT_PROVIDER.kind
    const extraBody = legacyThinkingExtraBody(kind, parsed.LLM_THINKING)
    console.log('[config] 开始迁移旧版 .env：')
    console.log(
      `  新增/更新服务商「${DEFAULT_PROVIDER.name}」：kind=${kind}，`
      + `base_url=${baseUrl || '（留空使用预设地址）'}，timeout_ms=${Number(parsed.LLM_TIMEOUT_MS) || DEFAULT_PROVIDER.timeout_ms}`,
    )
    if (parsed.LLM_MODEL) {
      console.log(`  新增/更新模型「chat」：model_identifier=${parsed.LLM_MODEL}，api_provider=${DEFAULT_PROVIDER.name}`)
      if (Object.keys(extraBody).length) {
        console.log(
          `  旧字段 LLM_THINKING=${parsed.LLM_THINKING} 已转换为模型「chat」的 `
          + `extra_body=${JSON.stringify(extraBody)}`,
        )
      } else if (parsed.LLM_THINKING) {
        console.log(`  移除旧字段 LLM_THINKING=${parsed.LLM_THINKING}（kind=${kind} 不使用该参数）`)
      }
      console.log('  新增/更新 chat 任务候选：model_list = ["chat"]')
    } else {
      console.log('  未创建模型：旧 .env 的 LLM_MODEL 为空')
    }
    console.log('[config] 旧版 .env 迁移完毕')
    return {
      api_providers: [{
        ...DEFAULT_PROVIDER,
        kind,
        base_url: baseUrl,
        api_key: parsed.LLM_API_KEY || '',
        timeout_ms: Number(parsed.LLM_TIMEOUT_MS) || DEFAULT_PROVIDER.timeout_ms,
      }],
      // 缺少模型 ID 时不创建无效候选；预填充只迁移已有值，完整性由设置页另行判断。
      models: parsed.LLM_MODEL ? [{
        name: 'chat',
        model_identifier: parsed.LLM_MODEL,
        api_provider: DEFAULT_PROVIDER.name,
        extra_body: extraBody,
        reasoning_parse_mode: 'field',
        visual: false, temperature: null, max_tokens: null, price_in: 0, price_out: 0,
        embedding_dim: 0,
      }] : [],
      model_tasks: {
        ...structuredClone(DEFAULT_CONFIG.model_tasks),
        chat: {
          ...structuredClone(DEFAULT_CONFIG.model_tasks.chat),
          model_list: parsed.LLM_MODEL ? ['chat'] : [],
          selection_strategy: 'sequential',
        },
      },
    }
  } catch (error) {
    throw new Error(`${envPath} 迁移失败：${error instanceof Error ? error.message : String(error)}`)
  }
}

/**
 * 把旧 `.env` 的预填结果合并进已有配置，只补充缺失字段，不覆盖用户已有内容。
 *
 * @param base 当前磁盘上的完整配置。
 * @param prefill `tryPrefillFromLegacyEnv` 返回的迁移预填值。
 * @returns 合并后的新配置对象。
 */
export function mergeLegacyEnvPrefill(
  base: YueliConfig,
  prefill: Partial<YueliConfig>,
): YueliConfig {
  const merged = structuredClone(base)
  const prefillProvider = prefill.api_providers?.[0]
  if (prefillProvider) {
    const existing = merged.api_providers.find((provider) => provider.name === prefillProvider.name)
    if (existing) {
      if (!existing.kind.trim()) existing.kind = prefillProvider.kind
      if (!existing.base_url.trim()) existing.base_url = prefillProvider.base_url
      if (!existing.api_key.trim()) existing.api_key = prefillProvider.api_key
      if (!existing.timeout_ms) existing.timeout_ms = prefillProvider.timeout_ms
    } else {
      merged.api_providers.push(structuredClone(prefillProvider))
    }
  }
  const prefillModel = prefill.models?.[0]
  if (prefillModel) {
    const existing = merged.models.find((model) => model.name === prefillModel.name)
    if (existing) {
      if (!existing.model_identifier.trim()) existing.model_identifier = prefillModel.model_identifier
      if (!existing.api_provider.trim()) existing.api_provider = prefillModel.api_provider
      // 用户已有 extra_body 的键优先；旧 .env 只补充尚不存在的思考参数。
      existing.extra_body = { ...prefillModel.extra_body, ...existing.extra_body }
    } else {
      merged.models.push(structuredClone(prefillModel))
    }
  }
  const prefillChat = prefill.model_tasks?.chat
  if (prefillChat) {
    const currentChat = merged.model_tasks.chat
    merged.model_tasks.chat = {
      ...currentChat,
      model_list: currentChat.model_list.length
        ? currentChat.model_list
        : prefillChat.model_list,
      selection_strategy: currentChat.model_list.length
        ? currentChat.selection_strategy
        : prefillChat.selection_strategy,
    }
  }
  return merged
}

/**
 * 从旧 `.env` 的 base_url 反推服务商预设。
 *
 * @param baseUrl 旧 `.env` 中填写的模型接口基地址。
 * @returns 识别到的 kind 名称；无法识别时返回空字符串，由调用方决定默认值。
 */
function inferLegacyProviderKind(baseUrl: string): string {
  const normalized = baseUrl.trim().toLowerCase()
  if (normalized.includes('api.deepseek.com')) return 'deepseek'
  if (normalized.includes('ark.cn-beijing.volces.com')) return 'ark'
  if (normalized.includes('dashscope.aliyuncs.com')) return 'dashscope'
  if (normalized.includes('api.moonshot.cn')) return 'moonshot'
  if (normalized.includes('open.bigmodel.cn')) return 'zhipu'
  if (normalized.includes('api.openai.com')) return 'openai'
  if (normalized.includes('ollama')) return 'ollama'
  return ''
}

/**
 * 把旧 `.env` 的 `LLM_THINKING` 转换为模型条目 `extra_body`。
 *
 * @param kind 服务商预设名称。
 * @param thinking 旧版 `disabled`、`enabled` 或 `auto` 值；可为空。
 * @returns 可直接合并进请求体的厂商参数对象；无需要迁移的思考参数时返回空对象。
 */
function legacyThinkingExtraBody(kind: string, thinking: string | undefined): Record<string, unknown> {
  const mode = (thinking || '').trim().toLowerCase()
  if (!mode) return {}
  // 这些厂商使用统一的 thinking 对象，auto 也按厂商可接受值原样保留。
  if (['ark', 'deepseek', 'moonshot', 'zhipu'].includes(kind)) {
    const type = mode === 'enabled' || mode === 'auto' ? mode : 'disabled'
    return { thinking: { type } }
  }
  // 百炼/DashScope 使用布尔开关；auto 与 enabled 统一按开启迁移。
  if (kind === 'dashscope') {
    return { enable_thinking: mode !== 'disabled' }
  }
  // 其余预设的旧运行链从未发送 thinking 字段，迁移时保持原请求体不变。
  return {}
}
