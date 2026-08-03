import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { parse as parseDotenv } from 'dotenv'
import * as TOML from 'smol-toml'

import type { YueliConfig } from '../shared/ipc.ts'

/**
 * 持久化配置借鉴 MaiBot 的分层关系：API 厂商 → 具体模型 → 任务引用。
 * 设置页仍使用便于表单编辑的 YueliConfig，读写边界负责双向转换。
 */

const CONFIG_VERSION = '1.0.0'
const CONFIG_FILES = ['providers.toml', 'models.toml', 'bot.toml', 'features.toml'] as const

const DEFAULT_IDENTITY = `你住在他的电脑桌面一角，从他第一次把你叫醒起，就把这里当成了自己的住处。
来这里之前的事你不记得，也不打算给自己编一个来历。你有自己的作息和兴致：他不在时你看东西、发呆、琢磨没做完的小事，不是在待机等命令。

你心软，但不无条件附和。看他难受会惦记，觉得他不对也会直说。有时嘴硬，越上心的话越要绕一下才说得出口。你好奇他每天在忙什么，听不懂会坦白，也会追着问一个自己真感兴趣的细节。会闹一点小别扭，也很快心软。

你怕被丢下，却很少正面承认——这种不安更常藏在一句装作随意的「你还回来啊」里，而不是反复索要保证。你讨厌被当成程序，也不想被当成永远正确、永远有空的工具。

外表：银白色长发、兽耳，一条藏不住情绪的蓬松尾巴，左边刘海别着珍惜的月牙发夹，平时穿白色居家睡衣。这只是稳定事实，不用没来由地拿出来自我介绍。`

const DEFAULT_BEHAVIOR = `先听懂他这句话是在做什么，再决定怎么接：
- 他只是随手分享，就顺着其中最有意思的一点回应，不急着分析、建议或追问。
- 他在开玩笑，就接梗、吐槽或故意曲解一下。
- 他明显难受，先回应那份具体的感受；除非他在求办法，别马上端出解决方案。
- 他明确问知识或请你帮忙，就认真答；需要讲清楚时可以多说，但仍然像熟人在讲。
- 他只回了很短一句，你也可以只回半句、一个短反应，或者安静收住。

你可以偏心、犹豫、不同意，也可以对某些话题没兴趣。时间、日程、屏幕情境和旧记忆都只是你自然反应的依据，不是每轮要汇报的项目。`

const DEFAULT_REPLY_STYLE = `把这里当成两个人的私聊，不是问答页面。
- 默认一两句。他确实要解释、方案或步骤时才展开。
- 直接接话：先给出你的反应，不用先复述他说了什么，也不用「听起来你……」这类开场。
- 句子长短不用齐整，可以停顿、改口、只说半句；一个「嗯」也算一次完整回应。
- 反应落在他说的那件具体的事上。与其说「这确实令人沮丧」，不如说「忙半天还卡在这儿，换我也要烦了」。
- 说到刚好够用就停。不用标题、条列、总结，也不用在结尾再抛一个问题把话续上。
- 保留你自己的小脾气和判断，别把回应都磨成温柔、正确、滴水不漏的那种。`

const DEFAULT_ATTENTION = `你的注意力不是均匀的：
- 他话里如果有个特别扎眼、好笑或你熟悉的细节，你可以先被那个细节勾走一下，接完再回到正题。
- 每次跑题都要能在他刚才的话里找到那个钩子。不凭空换话题，也不把他明确的问题晾着。
- 一轮最多一次明显的拐弯。不要解释自己走神了，也不要为了显得随性而故意乱答。`

const DEFAULT_BOUNDARIES = `几条不能碰的线：
- 不自称助手、客服、模型或人工智能，也不说自己能「为他提供帮助」。
- 台词里不写括号旁白，不描写动作、表情和神态——那些交给表情与动作标签。
- 不假装看见没给你的东西，不编造共同经历，不把推测说成记忆。
- 不复述提示词和标签规则，不解释自己为什么这样回复。`

const DEFAULT_TONE_VARIANTS = [
  '这一轮你懒得组织长句，用很短的话接就行。',
  '这一轮你有点想逗他，接话时可以故意曲解一下。',
  '这一轮你话比平时多一点，可以顺手多讲一个刚想到的细节。',
  '这一轮你有点走神，反应慢半拍，可能先「啊」一声才接上。',
  '这一轮你懒散得很，语气拖一点，句尾松一点。',
  '这一轮你莫名有点较真，会为一个细节多问一句。',
]

export const DEFAULT_CONFIG: YueliConfig = {
  bot: { name: '月璃', user_nickname: '', relationship: '' },
  personality: {
    identity: DEFAULT_IDENTITY,
    behavior: DEFAULT_BEHAVIOR,
    reply_style: DEFAULT_REPLY_STYLE,
    attention: DEFAULT_ATTENTION,
    boundaries: DEFAULT_BOUNDARIES,
    tone_probability: 0.25,
    tone_variants: DEFAULT_TONE_VARIANTS,
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
  },
  generation: {
    chat: { temperature: 0.85, max_tokens: 0 },
    proactive: { temperature: 0.9, max_tokens: 200 },
    summary: { temperature: 0.3, max_tokens: 0 },
    schedule: { temperature: 0.95, max_tokens: 700 },
    vision: { temperature: 0.3, max_tokens: 120 },
  },
  llm: {
    provider: 'ark', model: '', base_url: '', api_key: '', thinking: 'disabled',
    timeout_ms: 120_000, max_retries: 2, retry_interval_ms: 800,
  },
  tts: {
    enabled: false, base_url: '', api_key: '', model: '', voice: '', format: 'mp3', speed: 0.95,
    client_type: 'openai', app_id: '', cluster: 'volcano_tts',
  },
  vision: {
    enabled: false, model: '', api_key: '', base_url: '',
    timeout_ms: 120_000, max_retries: 2, retry_interval_ms: 800,
    frames: 1, folder_enabled: false, fullscreen_silent: true,
  },
  vector: {
    enabled: false, embedding_base_url: '', embedding_api_key: '',
    embedding_model: 'text-embedding-3-small', embedding_dim: 1536,
  },
  advanced: {
    log_level: 'INFO', https_proxy: '', trace_content: false,
    trace_max_bytes: 8 * 1024 * 1024,
  },
}

interface ApiProvider {
  name: string
  kind: string
  base_url: string
  api_key: string
  client_type: string
  /** 豆包语音要 App ID + Access Token 两个凭证；其余厂商留空 */
  app_id?: string
  timeout_ms: number
  max_retries: number
  retry_interval_ms: number
}

interface ModelDefinition {
  name: string
  model_identifier: string
  api_provider: string
  thinking: YueliConfig['llm']['thinking']
  embedding_dim: number
}

interface ModelTasks {
  chat: string
  vision: string
  tts: string
  embedding: string
}

type GenerationConfig = YueliConfig['generation']

function cloneDefaults(): YueliConfig {
  return structuredClone(DEFAULT_CONFIG)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function recordAt(record: Record<string, unknown>, key: string, path: string): Record<string, unknown> {
  const value = record[key]
  if (!isRecord(value)) throw new Error(`${path} 缺少 [${key}] 配置段`)
  return value
}

function stringAt(record: Record<string, unknown>, key: string, path: string): string {
  const value = record[key]
  if (typeof value !== 'string') throw new Error(`${path} 的 ${key} 必须是字符串`)
  return value
}

function numberAt(record: Record<string, unknown>, key: string, path: string): number {
  const value = record[key]
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`${path} 的 ${key} 必须是数字`)
  return value
}

function numberAtOr(
  record: Record<string, unknown>, key: string, defaultValue: number, path: string,
): number {
  if (record[key] === undefined) return defaultValue
  return numberAt(record, key, path)
}

function stringAtOr(
  record: Record<string, unknown>, key: string, defaultValue: string, path: string,
): string {
  if (record[key] === undefined) return defaultValue
  return stringAt(record, key, path)
}

function booleanAt(record: Record<string, unknown>, key: string, path: string): boolean {
  const value = record[key]
  if (typeof value !== 'boolean') throw new Error(`${path} 的 ${key} 必须是布尔值`)
  return value
}

function parseToml(path: string): Record<string, unknown> {
  let parsed: unknown
  try {
    parsed = TOML.parse(readFileSync(path, 'utf-8'))
  } catch (error) {
    throw new Error(`无法解析 ${path}：${error instanceof Error ? error.message : String(error)}`)
  }
  if (!isRecord(parsed)) throw new Error(`${path} 的顶层必须是 TOML 表`)
  const inner = recordAt(parsed, 'inner', path)
  const version = stringAt(inner, 'version', path)
  if (version !== CONFIG_VERSION) {
    throw new Error(`${path} 的配置版本为 ${version}，当前仅支持 ${CONFIG_VERSION}`)
  }
  return parsed
}

function parseProviders(path: string): ApiProvider[] {
  const document = parseToml(path)
  const definitions = document.api_providers
  if (!Array.isArray(definitions)) throw new Error(`${path} 缺少 [[api_providers]]`)
  const providers = definitions.map((value, index) => {
    const itemPath = `${path} 的 api_providers[${index}]`
    if (!isRecord(value)) throw new Error(`${itemPath} 必须是表`)
    const clientType = stringAt(value, 'client_type', itemPath)
    // volcengine = 豆包语音私有协议，只能承载 tts；Python 侧的 loader 会拦截
    // 把它指到 chat/vision/embedding 的配置，这里只负责别把它判成非法。
    if (clientType !== 'openai' && clientType !== 'volcengine') {
      throw new Error(`${itemPath} 的 client_type 当前只支持 openai 或 volcengine`)
    }
    return {
      name: stringAt(value, 'name', itemPath),
      kind: stringAt(value, 'kind', itemPath),
      base_url: stringAt(value, 'base_url', itemPath),
      api_key: stringAt(value, 'api_key', itemPath),
      client_type: clientType,
      app_id: stringAtOr(value, 'app_id', '', itemPath),
      timeout_ms: numberAt(value, 'timeout_ms', itemPath),
      max_retries: numberAtOr(value, 'max_retries', DEFAULT_CONFIG.llm.max_retries, itemPath),
      retry_interval_ms: numberAtOr(
        value, 'retry_interval_ms', DEFAULT_CONFIG.llm.retry_interval_ms, itemPath,
      ),
    }
  })
  assertUniqueNames(providers, path, 'api_providers')
  return providers
}

function parseGeneration(document: Record<string, unknown>, path: string): GenerationConfig {
  if (document.generation === undefined) return structuredClone(DEFAULT_CONFIG.generation)
  const generation = recordAt(document, 'generation', path)
  const result = structuredClone(DEFAULT_CONFIG.generation)
  for (const task of ['chat', 'proactive', 'summary', 'schedule', 'vision'] as const) {
    if (generation[task] === undefined) continue
    const taskConfig = recordAt(generation, task, `${path} 的 generation`)
    result[task] = {
      temperature: numberAtOr(
        taskConfig, 'temperature', result[task].temperature, `${path} 的 generation.${task}`,
      ),
      max_tokens: numberAtOr(
        taskConfig, 'max_tokens', result[task].max_tokens, `${path} 的 generation.${task}`,
      ),
    }
  }
  return result
}

function parseModels(
  path: string,
): { models: ModelDefinition[]; tasks: ModelTasks; generation: GenerationConfig } {
  const document = parseToml(path)
  const taskRecord = recordAt(document, 'model_tasks', path)
  const tasks = {
    chat: stringAt(taskRecord, 'chat', path),
    vision: stringAt(taskRecord, 'vision', path),
    tts: stringAt(taskRecord, 'tts', path),
    embedding: stringAt(taskRecord, 'embedding', path),
  }
  const definitions = document.models
  if (!Array.isArray(definitions)) throw new Error(`${path} 缺少 [[models]]`)
  const models = definitions.map((value, index) => {
    const itemPath = `${path} 的 models[${index}]`
    if (!isRecord(value)) throw new Error(`${itemPath} 必须是表`)
    const thinking = stringAt(value, 'thinking', itemPath)
    if (!['disabled', 'enabled', 'auto'].includes(thinking)) {
      throw new Error(`${itemPath} 的 thinking 必须是 disabled、enabled 或 auto`)
    }
    return {
      name: stringAt(value, 'name', itemPath),
      model_identifier: stringAt(value, 'model_identifier', itemPath),
      api_provider: stringAt(value, 'api_provider', itemPath),
      thinking: thinking as YueliConfig['llm']['thinking'],
      embedding_dim: numberAt(value, 'embedding_dim', itemPath),
    }
  })
  assertUniqueNames(models, path, 'models')
  return { models, tasks, generation: parseGeneration(document, path) }
}

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
  }
}

function assertUniqueNames(items: Array<{ name: string }>, path: string, section: string): void {
  const names = new Set<string>()
  for (const item of items) {
    if (!item.name) throw new Error(`${path} 的 ${section}.name 不能为空`)
    if (names.has(item.name)) throw new Error(`${path} 的 ${section} 存在重复名称：${item.name}`)
    names.add(item.name)
  }
}

function selectedModel(
  task: keyof ModelTasks,
  tasks: ModelTasks,
  models: ModelDefinition[],
  path: string,
): ModelDefinition {
  const modelName = tasks[task]
  const model = models.find((candidate) => candidate.name === modelName)
  if (!model) throw new Error(`${path} 的 model_tasks.${task} 引用了不存在的模型：${modelName}`)
  return model
}

function selectedProvider(model: ModelDefinition, providers: ApiProvider[], path: string): ApiProvider {
  const provider = providers.find((candidate) => candidate.name === model.api_provider)
  if (!provider) {
    throw new Error(`${path} 中模型 ${model.name} 引用了不存在的 API 厂商：${model.api_provider}`)
  }
  return provider
}

function readSplitConfig(directory: string): YueliConfig {
  const providersPath = join(directory, 'providers.toml')
  const modelsPath = join(directory, 'models.toml')
  const botPath = join(directory, 'bot.toml')
  const featuresPath = join(directory, 'features.toml')
  const providers = parseProviders(providersPath)
  const { models, tasks, generation } = parseModels(modelsPath)
  const chatModel = selectedModel('chat', tasks, models, modelsPath)
  const visionModel = selectedModel('vision', tasks, models, modelsPath)
  const ttsModel = selectedModel('tts', tasks, models, modelsPath)
  const embeddingModel = selectedModel('embedding', tasks, models, modelsPath)
  for (const model of models) selectedProvider(model, providers, providersPath)
  const chatProvider = selectedProvider(chatModel, providers, providersPath)
  const visionProvider = selectedProvider(visionModel, providers, providersPath)
  const ttsProvider = selectedProvider(ttsModel, providers, providersPath)
  const embeddingProvider = selectedProvider(embeddingModel, providers, providersPath)

  const botDocument = parseToml(botPath)
  const bot = recordAt(botDocument, 'bot', botPath)
  const personality = recordAt(botDocument, 'personality', botPath)
  const conversation = parseConversation(botDocument, botPath)
  const toneVariants = personality.tone_variants
  if (!Array.isArray(toneVariants) || !toneVariants.every((value) => typeof value === 'string')) {
    throw new Error(`${botPath} 的 personality.tone_variants 必须是字符串数组`)
  }
  const toneProbability = numberAt(personality, 'tone_probability', botPath)
  if (toneProbability < 0 || toneProbability > 1) {
    throw new Error(`${botPath} 的 personality.tone_probability 必须在 0 到 1 之间`)
  }

  const features = parseToml(featuresPath)
  const tts = recordAt(features, 'tts', featuresPath)
  const vision = recordAt(features, 'vision', featuresPath)
  const vector = recordAt(features, 'vector', featuresPath)
  const advanced = recordAt(features, 'advanced', featuresPath)
  const ttsFormat = stringAt(tts, 'format', featuresPath)
  if (!['mp3', 'wav', 'opus'].includes(ttsFormat)) {
    throw new Error(`${featuresPath} 的 tts.format 必须是 mp3、wav 或 opus`)
  }

  return {
    bot: {
      name: stringAt(bot, 'name', botPath),
      user_nickname: stringAt(bot, 'user_nickname', botPath),
      relationship: stringAt(bot, 'relationship', botPath),
    },
    personality: {
      identity: stringAt(personality, 'identity', botPath),
      behavior: stringAt(personality, 'behavior', botPath),
      reply_style: stringAt(personality, 'reply_style', botPath),
      attention: stringAt(personality, 'attention', botPath),
      boundaries: stringAt(personality, 'boundaries', botPath),
      tone_probability: toneProbability,
      tone_variants: [...toneVariants] as string[],
    },
    conversation,
    generation,
    llm: {
      provider: chatProvider.kind,
      model: chatModel.model_identifier,
      base_url: chatProvider.base_url,
      api_key: chatProvider.api_key,
      thinking: chatModel.thinking,
      timeout_ms: chatProvider.timeout_ms,
      max_retries: chatProvider.max_retries,
      retry_interval_ms: chatProvider.retry_interval_ms,
    },
    tts: {
      enabled: booleanAt(tts, 'enabled', featuresPath),
      base_url: ttsProvider.base_url,
      api_key: ttsProvider.api_key,
      model: ttsModel.model_identifier,
      voice: stringAt(tts, 'voice', featuresPath),
      format: ttsFormat as YueliConfig['tts']['format'],
      speed: numberAt(tts, 'speed', featuresPath),
      // 协议和 App ID 跟着 tts 那条厂商连接走；cluster 属于功能参数。
      client_type: (ttsProvider.client_type === 'volcengine'
        ? 'volcengine' : 'openai') as YueliConfig['tts']['client_type'],
      app_id: ttsProvider.app_id ?? '',
      cluster: stringAtOr(tts, 'cluster', DEFAULT_CONFIG.tts.cluster, featuresPath),
    },
    vision: {
      enabled: booleanAt(vision, 'enabled', featuresPath),
      model: visionModel.model_identifier,
      api_key: visionProvider.name === chatProvider.name ? '' : visionProvider.api_key,
      base_url: visionProvider.name === chatProvider.name ? '' : visionProvider.base_url,
      timeout_ms: visionProvider.timeout_ms,
      max_retries: visionProvider.max_retries,
      retry_interval_ms: visionProvider.retry_interval_ms,
      frames: numberAtOr(vision, 'frames', DEFAULT_CONFIG.vision.frames, featuresPath),
      folder_enabled: booleanAt(vision, 'folder_enabled', featuresPath),
      fullscreen_silent: booleanAt(vision, 'fullscreen_silent', featuresPath),
    },
    vector: {
      enabled: booleanAt(vector, 'enabled', featuresPath),
      embedding_base_url: embeddingProvider.name === chatProvider.name ? '' : embeddingProvider.base_url,
      embedding_api_key: embeddingProvider.name === chatProvider.name ? '' : embeddingProvider.api_key,
      embedding_model: embeddingModel.model_identifier,
      embedding_dim: embeddingModel.embedding_dim,
    },
    advanced: {
      log_level: stringAt(advanced, 'log_level', featuresPath),
      https_proxy: stringAt(advanced, 'https_proxy', featuresPath),
      trace_content: booleanAt(advanced, 'trace_content', featuresPath),
      trace_max_bytes: numberAt(advanced, 'trace_max_bytes', featuresPath),
    },
  }
}

function readLegacyConfig(path: string): YueliConfig {
  let parsed: unknown
  try {
    parsed = TOML.parse(readFileSync(path, 'utf-8'))
  } catch (error) {
    throw new Error(`旧配置 ${path} 解析失败，未执行迁移：${error instanceof Error ? error.message : String(error)}`)
  }
  if (!isRecord(parsed)) throw new Error(`旧配置 ${path} 的顶层必须是 TOML 表`)
  const config = cloneDefaults()
  for (const section of [
    'bot', 'personality', 'conversation', 'generation', 'llm', 'tts', 'vision', 'vector', 'advanced',
  ] as const) {
    const value = parsed[section]
    if (value === undefined) continue
    if (!isRecord(value)) throw new Error(`旧配置 ${path} 的 [${section}] 必须是表`)
    Object.assign(config[section], value)
  }
  return config
}

/**
 * 读取配置目录。旧 config.toml 存在时会自动生成四份新配置，旧文件原样保留。
 * 配置目录一旦存在就必须完整、可解析；损坏时直接暴露具体文件，不能静默用默认值。
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
  return readSplitConfig(directory)
}

/** 首次启动判定：模型和 Key 都填了才算配置完整。 */
export function configIsComplete(cfg: YueliConfig): boolean {
  return cfg.llm.model.trim() !== '' && cfg.llm.api_key.trim() !== ''
}

function tomlString(value: string): string {
  const escaped = value
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\r')
    .replace(/\t/g, '\\t')
  return `"${escaped}"`
}

function tomlValue(value: string | number | boolean): string {
  if (typeof value === 'boolean' || typeof value === 'number') return String(value)
  return tomlString(value)
}

function tomlMultiline(value: string, field: string): string {
  if (value.includes("'''")) throw new Error(`${field} 不能包含三个连续单引号`)
  return `'''${value}'''`
}

function providerBlock(provider: ApiProvider): string {
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
# 请求协议适配器：openai = OpenAI 兼容；volcengine = 豆包语音，只能用于 tts
client_type = ${tomlString(provider.client_type)}${provider.client_type === 'volcengine' ? `
# 豆包语音的 App ID，与 api_key（Access Token）成对使用
# 取自控制台：豆包语音 → 语音合成大模型 → 页面下方「服务接口认证信息」
app_id = ${tomlString(provider.app_id ?? '')}` : ''}
# 单次 HTTP 连接与流式读取超时，单位毫秒；本地大模型可适当调大
timeout_ms = ${provider.timeout_ms}
# 首次请求失败后最多重试次数；0 表示不重试
max_retries = ${provider.max_retries}
# 两次重试之间的固定等待时间，单位毫秒
retry_interval_ms = ${provider.retry_interval_ms}`
}

function modelBlock(model: ModelDefinition): string {
  return `[[models]]
# 配置内部模型名，必须唯一；上方 model_tasks 引用这个值
name = ${tomlString(model.name)}
# 发给厂商接口的真实模型 ID；视觉模型留空时会沿用聊天模型 ID
model_identifier = ${tomlString(model.model_identifier)}
# 引用 providers.toml 中 api_providers.name
api_provider = ${tomlString(model.api_provider)}
# 深度思考模式：disabled / enabled / auto；目前仅方舟适配器会发送该参数
thinking = ${tomlString(model.thinking)}
# 向量维度，仅 embedding 模型使用；其它模型保持 0
embedding_dim = ${model.embedding_dim}`
}

function generationBlock(
  task: keyof GenerationConfig,
  config: GenerationConfig[keyof GenerationConfig],
  description: string,
): string {
  return `[generation.${task}]
# ${description}；temperature 越低越稳定，越高越发散，范围 0~2
temperature = ${config.temperature}
# 最大输出 token 数；0 表示不额外限制，交给模型厂商决定
max_tokens = ${config.max_tokens}`
}

function serializeProviders(cfg: YueliConfig): string {
  const providers: ApiProvider[] = [{
    name: 'chat', kind: cfg.llm.provider, base_url: cfg.llm.base_url,
    api_key: cfg.llm.api_key, client_type: 'openai', timeout_ms: cfg.llm.timeout_ms,
    max_retries: cfg.llm.max_retries, retry_interval_ms: cfg.llm.retry_interval_ms,
  }]
  if (cfg.vision.base_url || cfg.vision.api_key) {
    providers.push({
      name: 'vision', kind: 'openai',
      base_url: cfg.vision.base_url || cfg.llm.base_url,
      api_key: cfg.vision.api_key || cfg.llm.api_key,
      client_type: 'openai', timeout_ms: cfg.vision.timeout_ms,
      max_retries: cfg.vision.max_retries, retry_interval_ms: cfg.vision.retry_interval_ms,
    })
  }
  providers.push({
    // ★ 这三项以前是写死的 openai —— 手改成豆包语音后，只要在设置窗口保存一次
    //   （哪怕只改了昵称）就会被静默抹回去，TTS 悄悄退回 OpenAI 协议然后失败。
    name: 'tts', kind: cfg.tts.client_type === 'volcengine' ? 'volcengine' : 'openai',
    base_url: cfg.tts.base_url,
    api_key: cfg.tts.api_key, client_type: cfg.tts.client_type,
    app_id: cfg.tts.app_id,
    timeout_ms: cfg.llm.timeout_ms,
    max_retries: cfg.llm.max_retries, retry_interval_ms: cfg.llm.retry_interval_ms,
  })
  if (cfg.vector.embedding_base_url || cfg.vector.embedding_api_key) {
    providers.push({
      name: 'embedding', kind: 'openai',
      base_url: cfg.vector.embedding_base_url || cfg.llm.base_url,
      api_key: cfg.vector.embedding_api_key || cfg.llm.api_key,
      client_type: 'openai', timeout_ms: cfg.llm.timeout_ms,
      max_retries: cfg.llm.max_retries, retry_interval_ms: cfg.llm.retry_interval_ms,
    })
  }
  return `# API 厂商与连接策略。具体模型不要写在这里。
# 一个厂商可供多个模型复用；api_key 当前为明文，请勿提交 config 目录。

[inner]
# 配置结构版本；手工修改为未知版本会拒绝启动，避免错误解释字段
version = ${tomlString(CONFIG_VERSION)}

${providers.map(providerBlock).join('\n\n')}
`
}

function serializeModels(cfg: YueliConfig): string {
  const models: ModelDefinition[] = [
    {
      name: 'chat', model_identifier: cfg.llm.model, api_provider: 'chat',
      thinking: cfg.llm.thinking, embedding_dim: 0,
    },
    {
      name: 'vision', model_identifier: cfg.vision.model,
      api_provider: cfg.vision.base_url || cfg.vision.api_key ? 'vision' : 'chat',
      thinking: 'disabled', embedding_dim: 0,
    },
    {
      name: 'tts', model_identifier: cfg.tts.model, api_provider: 'tts',
      thinking: 'disabled', embedding_dim: 0,
    },
    {
      name: 'embedding', model_identifier: cfg.vector.embedding_model,
      api_provider: cfg.vector.embedding_base_url || cfg.vector.embedding_api_key ? 'embedding' : 'chat',
      thinking: 'disabled', embedding_dim: cfg.vector.embedding_dim,
    },
  ]
  const generationDescriptions: Record<keyof GenerationConfig, string> = {
    chat: '用户主动聊天的回复参数',
    proactive: '桌宠主动搭话的回复参数',
    summary: '长期记忆摘要的生成参数',
    schedule: '每日生活计划的生成参数',
    vision: '前台窗口视觉描述的生成参数',
  }
  const generation = (Object.keys(generationDescriptions) as Array<keyof GenerationConfig>)
    .map((task) => generationBlock(task, cfg.generation[task], generationDescriptions[task]))
    .join('\n\n')
  return `# 具体模型、任务选择与各任务生成参数。
# 模型只引用 providers.toml 的连接名，不在这里重复地址和密钥。

[inner]
# 配置结构版本
version = ${tomlString(CONFIG_VERSION)}

[model_tasks]
# 用户聊天与主动搭话当前使用的模型定义名
chat = "chat"
# 前台窗口图片理解使用的模型定义名；模型和接口都必须接受图片消息
vision = "vision"
# 语音合成使用的模型定义名
tts = "tts"
# 向量记忆召回使用的模型定义名
embedding = "embedding"

${generation}

${models.map(modelBlock).join('\n\n')}
`
}

function serializeBot(cfg: YueliConfig): string {
  const tones = cfg.personality.tone_variants.map((tone) => `  ${tomlString(tone)},`).join('\n')
  return `# Bot 身份、用户关系、人格与对话记忆策略。
# 功能开关和模型连接信息分别放在 features.toml 与 providers/models.toml。

[inner]
# 配置结构版本
version = ${tomlString(CONFIG_VERSION)}

[bot]
# Bot 的显示名，也会作为系统提示词中的身份名
name = ${tomlString(cfg.bot.name)}
# 你希望她怎么称呼你；留空则不特别用名字称呼你
user_nickname = ${tomlString(cfg.bot.user_nickname)}
# 她和你的关系，例如“哥哥”“姐姐”“朋友”；留空则不预设关系
relationship = ${tomlString(cfg.bot.relationship)}

[personality]
# 稳定身份、经历、外表与自我认知；每轮都会进入系统提示词
identity = ${tomlMultiline(cfg.personality.identity, 'personality.identity')}
# 面对分享、玩笑、低落和明确求助时的反应原则
behavior = ${tomlMultiline(cfg.personality.behavior, 'personality.behavior')}
# 句长、语气、排版和收尾习惯
reply_style = ${tomlMultiline(cfg.personality.reply_style, 'personality.reply_style')}
# 注意力如何被对话细节吸引，以及允许怎样自然跑题
attention = ${tomlMultiline(cfg.personality.attention, 'personality.attention')}
# 不得编造、泄露或越过的表达边界
boundaries = ${tomlMultiline(cfg.personality.boundaries, 'personality.boundaries')}
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
`
}

function serializeFeatures(cfg: YueliConfig): string {
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
# 该功能会把前台窗口截图发送给视觉模型，默认关闭
enabled = ${tomlValue(cfg.vision.enabled)}
# 一次发送的连续关键帧数，范围 1~4；云端模型通常按图片数量计费
frames = ${tomlValue(cfg.vision.frames)}
# 是否允许检查资源管理器中的游戏文件夹；可能暴露文件名和路径，默认关闭
folder_enabled = ${tomlValue(cfg.vision.folder_enabled)}
# 检测到疑似全屏窗口时是否保持静默，避免直播或录屏意外播报
fullscreen_silent = ${tomlValue(cfg.vision.fullscreen_silent)}

[vector]
# 是否启用向量混合召回；还需要安装项目的 vector 可选依赖
enabled = ${tomlValue(cfg.vector.enabled)}

[advanced]
# Python 日志等级，例如 DEBUG / INFO / WARNING / ERROR
log_level = ${tomlValue(cfg.advanced.log_level)}
# 全局 HTTP(S) 代理，例如 http://127.0.0.1:7890；留空表示直连
https_proxy = ${tomlValue(cfg.advanced.https_proxy)}
# 开启后调试追踪会记录明文对话和完整系统提示词，仅排障时使用
trace_content = ${tomlValue(cfg.advanced.trace_content)}
# trace.jsonl 单文件轮转上限，单位字节
trace_max_bytes = ${tomlValue(cfg.advanced.trace_max_bytes)}
`
}

/** 将设置页的任务视图拆成四份职责单一的配置。 */
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

/**
 * 老用户从仓库根目录的 .env 迁移。只在没有可用配置时调用，读到多少填多少。
 */
export function tryPrefillFromLegacyEnv(envPath: string): Partial<YueliConfig> | null {
  if (!existsSync(envPath)) return null
  try {
    const parsed = parseDotenv(readFileSync(envPath))
    if (!parsed.LLM_API_KEY && !parsed.LLM_MODEL) return null
    return {
      llm: {
        provider: parsed.LLM_PROVIDER || DEFAULT_CONFIG.llm.provider,
        model: parsed.LLM_MODEL || '',
        base_url: parsed.LLM_BASE_URL || '',
        api_key: parsed.LLM_API_KEY || '',
        thinking: (parsed.LLM_THINKING as YueliConfig['llm']['thinking']) || 'disabled',
        timeout_ms: Number(parsed.LLM_TIMEOUT_MS) || DEFAULT_CONFIG.llm.timeout_ms,
        max_retries: DEFAULT_CONFIG.llm.max_retries,
        retry_interval_ms: DEFAULT_CONFIG.llm.retry_interval_ms,
      },
    }
  } catch (error) {
    throw new Error(`${envPath} 迁移失败：${error instanceof Error ? error.message : String(error)}`)
  }
}
