/**
 * 定义 Electron 主进程、preload bridge 和各渲染窗口共享的 IPC 通道及数据结构。
 *
 * 主进程负责实现通道，preload 只暴露受限方法，渲染层依赖本文件的类型完成
 * 编译期约束；聊天事件、日程、观察快照和配置结构均在此处统一声明，避免两端
 * 使用未同步的字符串字段。
 */

/** 流式响应解析事件（Python 侧通过 WS 推来，主进程原样转发）。 */
export type ParseEvent =
  | { type: 'say'; emotion?: string; gesture?: string }
  | { type: 'text'; value: string }
  | { type: 'sayEnd' }
  | { type: 'memory'; memoryType?: string; content: string }
  | { type: 'mood'; favor?: number; energy?: number }

/** 每日方向中的一条主线意向，不包含任何执行钟点。 */
export interface DayPlanIntention {
  what: string
  carriedDays: number
}

/** 每日方向（Python 侧产物，通过观察与日记接口下发）。 */
export interface DayPlan {
  date: string
  theme: string
  intentions: DayPlanIntention[]
  roughRhythm: string
}

export interface ActivitySnapshot {
  id: number
  kind: 'awake' | 'rest' | 'sleep'
  doing: string
  mood: string
  energyPace: number
  moodPace: number
  advances: number | null
  startedAt: number
  expectedUntil: number
  endedAt?: number | null
  source: 'decided' | 'backfilled' | 'interrupted'
}

export interface IntentionProgress {
  index: number
  what: string
  carriedDays: number
  advanced: boolean
}

/** 观察面板中一条可切换的会话流。 */
export interface ObservabilityStream {
  id: number
  platform: string
  kind: 'desktop' | 'direct' | 'group'
  externalId: string
}

export interface ObservabilityStreamsPayload {
  streams: ObservabilityStream[]
}

/** 人物好感度；全局精力不属于任何单个人物。 */
export interface PersonBond {
  intimacy: number
  updatedAt: number
}

/** L3 长期事实。HTTP 契约统一使用 camelCase，禁止前端猜 Python 字段名。 */
export interface ObservabilityFact {
  id: number
  kind: string
  content: string
  retention: number
  score: number
  dueAt: number
  frozen: boolean
}

export interface PersonIdentity {
  platform: string
  externalId: string
  displayName: string
}

export interface GroupMembership {
  streamId: number
  groupExternalId: string
  groupCard: string
}

export interface PersonSummary {
  id: number
  kind: 'owner' | 'contact'
  displayName: string
  firstSeenAt: number
  /** 好感度（`persona_bond.intimacy`）。 */
  intimacy: number
  /** 事实条数；`active` 为当前生效条数，`total` 含已冻结。 */
  factCount: { total: number; active: number }
  /**
   * `persona_bond.updated_at` 毫秒时间戳。对 contact 即最后互动时间；
   * owner 行会被每小时结算推进，不代表互动，界面不要按「最后互动」渲染。
   */
  bondUpdatedAt: number
  identities: PersonIdentity[]
  streams: ObservabilityStream[]
  groupMemberships: GroupMembership[]
}

export interface ConversationParticipant {
  id: number
  kind: 'owner' | 'contact'
  displayName: string
  externalId: string
  nickname: string
  groupCard: string
}

export interface PersonProfile extends PersonSummary {
  bond: PersonBond
  facts: ObservabilityFact[]
}

export interface PersonsPayload {
  persons: PersonSummary[]
}

/** Python `/observability` 的完整跨语言契约。 */
export interface ObservabilityPayload {
  now: number
  selfState: {
    energy: number
    mood: number
    statusLabel: string
  }
  schedule: DayPlan | null
  activity?: ActivitySnapshot
  activityTimeline?: ActivitySnapshot[]
  intentionProgress?: IntentionProgress[]
  conversation: {
    workingMessages: number
    participants: ConversationParticipant[]
  }
  sleep?: Record<string, unknown>
  impulse?: Record<string, unknown>
  sensing?: Record<string, unknown>
  voice?: {
    enabled: boolean
    configured: boolean
    model: string
    voice: string
    failures: number
    cacheHits: number
    cacheMisses: number
    cache: { files: number; bytes: number }
  }
}

/** `/ws/events` 与 `/events` 事件载荷的稳定公共字段；各 kind 的业务字段保留为 unknown。 */
export interface TraceEntry {
  seq: number | null
  at: number
  kind: string
  streamId?: number | null
  platform?: string
  personId?: number
  personKind?: string
  senderExternalId?: string
  senderNickname?: string
  senderGroupCard?: string
  senderDisplayName?: string
  senderLabel?: string
  botName?: string
  turnId?: number | null
  [key: string]: unknown
}
export const IPC = {
  /** 渲染层 → 主进程：切换窗口是否吃鼠标事件（点击穿透） */
  SetInteractive: 'pet:set-interactive',
  /** 渲染层 → 主进程：退出应用 */
  Quit: 'app:quit',

  /** 渲染层 → 主进程：开始/结束拖动窗口 */
  BeginDrag: 'pet:begin-drag',
  EndDrag: 'pet:end-drag',
  /** 渲染层 → 主进程：请求输入栏获得或释放窗口焦点。 */
  FocusInput: 'pet:focus-input',
  /** 渲染层 → 主进程：用户与角色交互，暂时保持唤醒状态。 */
  UserInteracted: 'pet:user-interacted',

  /** 渲染层 → 主进程：发一句话并等待后端确认进入缓冲。 */
  Send: 'chat:send',
  /** 渲染层 → 主进程：中断当前正在输出的回复。 */
  Interrupt: 'chat:interrupt',
  /** 主进程 → 渲染层：本轮的流式事件 */
  Event: 'chat:event',
  /** 主进程 → 渲染层：从托盘菜单唤起输入栏 */
  OpenComposer: 'chat:open-composer',
  /** 主进程 → 渲染层：一段待播放的语音，或一个停止信号 */
  Voice: 'voice:play',
  /** 主进程 → 渲染层：一张截图正在发往视觉模型。 */
  Vision: 'vision:watching',
  /** 主进程 → 渲染层：唯一的睡眠状态，不传日程和时钟细节。 */
  Sleep: 'sleep:state',
  /** 日记窗口 → 主进程：取全部情节记录 */
  Diary: 'diary:list',
  /** 设置窗口 → 主进程：读取拆分后的配置目录（不存在则返回默认值）。 */
  ReadConfig: 'settings:read-config',
  /** 设置窗口 → 主进程：写入拆分配置；首次启动时这个调用成功后才会继续正常启动流程。 */
  SaveConfig: 'settings:save-config',
  /** 设置窗口 → 主进程：重启 Python 后端，使刚保存的配置生效。 */
  RestartBackend: 'settings:restart-backend',
} as const

/**
 * 推给渲染层的对话事件。
 *
 * 每个事件携带 turnId，用于丢弃被打断轮次的迟到增量，
 * 防止上一轮流式输出写入新一轮消息气泡。
 */
export type ChatStreamEvent =
  | { turnId: number; kind: 'start' }
  | { turnId: number; kind: 'parse'; event: ParseEvent }
  | { turnId: number; kind: 'done' }
  | { turnId: number; kind: 'silent'; reason: string }
  | { turnId: number; kind: 'error'; message: string; hint?: string }

/**
 * 语音事件。音频走 base64 而不是 ArrayBuffer —— Electron 的结构化克隆
 * 对 Buffer 的处理在各版本间存在差异，base64 能提供稳定的跨进程序列化形态。
 */
export type VoiceEvent =
  | { turnId: number; kind: 'audio'; format?: string; data?: string }
  | { turnId: number; kind: 'stop' }

/** 视觉请求状态。只传布尔值，不传截图、窗口标题或模型输出。 */
export interface VisionWatchEvent {
  watching: boolean
}

/** 睡眠状态事件。计算只在主进程进行，渲染层只据此切换立绘。 */
export interface SleepStateEvent {
  asleep: boolean
}

/** 日记里的一条。kind 区分对话摘要、梦、离线补偿。 */
export interface DiaryEntry {
  id: number
  kind: string
  summary: string
  endedAt: number
  /** 触发回忆该条内容的情境线索。 */
  cues: string[]
}

/** 日记内容与主进程统一业务时钟一起下发，渲染层不应自行读取 Date.now()。 */
export interface DiaryPayload {
  entries: DiaryEntry[]
  /** 角色当天的生活计划；日记以第一人称呈现，不暴露人格数值。 */
  today: DayPlan
  /** 全部 L3 记忆，已经按留存度排序；冻结项会单独呈现。 */
  memories: Array<{ content: string; frozen: boolean }>
  /** 有至少一条历史快照且模型生成合格时才存在。 */
  change?: string
  now: number
}

/** preload 暴露给渲染层的 API 形状。 */
export interface PetBridge {
  setInteractive(interactive: boolean): void
  quit(): void
  beginDrag(): void
  endDrag(): void
  focusInput(focus: boolean): void
  userInteracted(): void
  send(text: string): Promise<void>
  interrupt(): void
  onEvent(handler: (e: ChatStreamEvent) => void): () => void
  /** 主进程要求打开输入栏。 */
  onOpenComposer(handler: () => void): () => void
  /** 收到一段语音（base64）或停止信号。 */
  onVoice(handler: (e: VoiceEvent) => void): () => void
  /** 视觉请求进行时显示克制的知情提示。 */
  onVisionWatching(handler: (e: VisionWatchEvent) => void): () => void
  /** 主进程推送睡眠状态；渲染层不能自行读取时钟。 */
  onSleepState(handler: (e: SleepStateEvent) => void): () => void
}

/** 日记窗口的最小只读 bridge。它与桌宠 bridge 隔离，绝不带交互写入方法。 */
export interface DiaryBridge {
  read(): Promise<DiaryPayload>
}

/** 请求协议适配器：openai = OpenAI 兼容；volcengine = 豆包语音私有协议，只能用于 tts。 */
export type ClientType = 'openai' | 'volcengine'
export type AuthType = 'bearer' | 'header' | 'query' | 'none'
export type ReasoningParseMode = 'field' | 'tag' | 'none'

/**
 * 任务在多个候选模型之间的挑选顺序。
 *   sequential = 按列表顺序，永远优先第一条（主备）
 *   random     = 每次随机起点，把流量摊到多家（分摊额度）
 *   balance    = 在健康候选之间逐轮轮询，稳定地分摊请求
 * 三种策略都遵守同一条熔断规则：刚失败过的厂商在冷却期内排到最后。
 */
export type SelectionStrategy = 'sequential' | 'random' | 'balance'

/** providers.toml 里的一条可复用连接。一个厂商可供多个模型引用。 */
export interface ApiProviderConfig {
  /** 配置内部引用名，必须唯一；模型的 api_provider 写这个值 */
  name: string
  /** 厂商预设名，base_url 留空时用于选择内置官方地址 */
  kind: string
  base_url: string
  api_key: string
  auth_type: AuthType
  auth_name: string
  client_type: ClientType
  /** 仅 volcengine：App ID，与 api_key（Access Token）成对使用 */
  app_id: string
  /** 模型列表端点，用于 WebUI 连通性测试与模型拉取；OpenAI 兼容默认 /models */
  model_list_endpoint: string
  /** 中转头等需要额外 HTTP 头的厂商在这里写键值；认证头仍由 auth_* 负责 */
  default_headers: Record<string, string>
  /** 中转头等需要固定查询参数的厂商在这里写键值 */
  default_query: Record<string, string>
  timeout_ms: number
  max_retries: number
  retry_interval_ms: number
}

/** models.toml 里的一个具体模型，只引用厂商名，不重复地址和密钥。 */
export interface ModelDefinitionConfig {
  /** 配置内部模型名，必须唯一；任务的 model_list 写这个值 */
  name: string
  /** 发给厂商接口的真实模型 ID */
  model_identifier: string
  api_provider: string
  extra_body: Record<string, unknown>
  reasoning_parse_mode: ReasoningParseMode
  /** 视觉能力标记：只有 visual = true 的模型才能进入 vision / 图片描述任务 */
  visual: boolean
  /** 可选模型级温度覆盖；留空（null）时使用任务 generation 配置 */
  temperature: number | null
  /** 可选模型级最大输出覆盖；留空（null）时使用任务 generation 配置 */
  max_tokens: number | null
  /** 计费参考价，单位元/百万 token；仅用于 WebUI 展示 */
  price_in: number
  price_out: number
  /** 向量维度，仅 embedding 模型使用 */
  embedding_dim: number
}

/** 一个任务的候选模型列表与轮询策略。列表里排第一的是主力。 */
export interface TaskRoutingConfig {
  model_list: string[]
  selection_strategy: SelectionStrategy
  first_token_timeout_ms: number
  slow_threshold_ms: number
}

/**
 * 设置页使用的任务视图。持久化层会把它拆成 providers/models/bot/features
 * 四份 TOML，Python 侧再组合成同样的运行时结构。
 */
export interface YueliConfig {
  bot: { name: string; aliases: string[]; user_nickname: string; relationship: string }
  group_chat: {
    at_mention_must_reply: boolean
    name_mention_probability: number
    presence_decay_strength: number
    persona_weight: number
    reply_window_minutes: number
    max_replies_in_window: number
    /** 允许对群消息贴表情回应 */
    reactions_enabled: boolean
    /** 允许使用 QQ 戳一戳 */
    pokes_enabled: boolean
    /** 允许她主动起话头（不接任何人的话） */
    self_started_topics: boolean
    /** 观察任务刷新场景画像前跳过的消息条数；0 表示不刷新 */
    scene_refresh_messages: number
  }
  schedule: {
    sleep_enabled: boolean
    fallback_theme: string
    generation_retry_interval_minutes: number
  }
  personality: {
    birthday: string
    personality: string
    reply_style: string
    tone_probability: number
    tone_variants: string[]
    expression_habits: string[]
    proactive_expression_habits: string[]
  }
  conversation: {
    working_memory_messages: number
    summarize_trigger_messages: number
    summarize_batch_messages: number
    session_gap_minutes: number
    fact_recall_limit: number
    recalled_episode_limit: number
    recent_episode_limit: number
    episode_context_limit: number
    /** 未抽取消息达到此数量后触发一次事实抽取 */
    fact_extract_trigger_messages: number
    /** 每次事实抽取消化的最老消息条数 */
    fact_extract_batch_messages: number
  }
  /** 对话 Agent（决策/表达分离）的运行模式与触发参数。 */
  conversation_agent: {
    mode: 'off' | 'shadow' | 'selected_streams' | 'enabled'
    selected_streams: string[]
    trigger_mode: 'signal' | 'frequency' | 'reply_necessity'
    frequency_talk_value: number
    reply_necessity_threshold: number
    max_cognitive_rounds: number
    split_replyer: boolean
    tool_calling: boolean
  }
  /** 气泡拆分与打字节奏。 */
  typing: {
    bubble_target_chars: number
    max_bubbles_per_say: number
    delay_enabled: boolean
    chinese_char_seconds: number
    latin_char_seconds: number
    send_gap_seconds: number
    max_delay_seconds: number
    emoji_pick_seconds: number
    follow_up: { enabled: boolean; peer_silence_minutes: number }
    nudge: { enabled: boolean; peer_silence_minutes: number; max_per_silence: number }
  }
  /** 表情包库的容量、收集与淘汰参数。 */
  emoji: {
    /** 可发送表情最大条数；0 表示不限 */
    max_count: number
    /** 库满后是否自动淘汰最冷的条目 */
    auto_evict: boolean
    /** 两次库容量检查之间的最小间隔（分钟） */
    check_interval_minutes: number
    /** 收集时的单文件大小上限（MB）；0 表示不限 */
    max_file_size_mb: number
    /** 入库前是否调用视觉模型审查内容 */
    content_filtration: boolean
    /** 是否从聊天里自动收集表情包 */
    collect_enabled: boolean
    /** 孤儿文件清理任务节奏 */
    cleanup: {
      /** 是否定期清理孤儿文件 */
      enabled: boolean
      /** 两次清理检查之间的最小间隔（小时） */
      check_interval_hours: number
      /** 孤儿文件至少保留多少天 */
      orphan_retention_days: number
    }
  }
  generation: {
    chat: { temperature: number; max_tokens: number }
    proactive: { enabled: boolean; temperature: number; max_tokens: number }
    summary: { temperature: number; max_tokens: number }
    expression: { temperature: number; max_tokens: number }
    schedule: { temperature: number; max_tokens: number }
    vision: { temperature: number; max_tokens: number }
    planner: { temperature: number; max_tokens: number }
    replyer: { temperature: number; max_tokens: number }
    scene: { temperature: number; max_tokens: number }
    memory: { temperature: number; max_tokens: number }
  }
  /** 所有可用连接。轮询就是在这些连接之间换。 */
  api_providers: ApiProviderConfig[]
  /** 所有模型定义。同一个厂商可以有多个模型，同一个模型 ID 也能挂在多个厂商下。 */
  models: ModelDefinitionConfig[]
  /** 各类任务各自的候选模型与轮询策略。 */
  model_tasks: {
    chat: TaskRoutingConfig
    proactive: TaskRoutingConfig
    summary: TaskRoutingConfig
    schedule: TaskRoutingConfig
    vision: TaskRoutingConfig
    expression: TaskRoutingConfig
    planner: TaskRoutingConfig
    replyer: TaskRoutingConfig
    scene: TaskRoutingConfig
    memory: TaskRoutingConfig
    tts: TaskRoutingConfig
    embedding: TaskRoutingConfig
  }
  tts: {
    enabled: boolean; voice: string
    format: 'mp3' | 'wav' | 'opus'; speed: number
    /** 仅 volcengine：集群名，默认 volcano_tts */
    cluster: string
  }
  vision: {
    enabled: boolean
    /** 允许理解 QQ 聊天里收到的图片 */
    chat_image_enabled: boolean
    fullscreen_silent: boolean
    /** window = 只截前台那一个窗口；screen = 截整个主屏（能看到桌面，但会连带截到别的窗口） */
    capture_mode: 'window' | 'screen'
  }
  perception: {
    surfaces: Array<'desktop' | 'direct'>
  }
  vector: {
    enabled: boolean
  }
  log: {
    level: string; console_level: string; file_level: string
    level_style: 'lite' | 'compact' | 'full'
    color_scope: 'none' | 'title' | 'full'
    date_format: string
    to_file: boolean
    file_max_bytes: number; max_files: number; cleanup_days: number
    library_levels: Record<string, string>
    suppress_libraries: string[]
    request_snapshots: boolean; max_snapshot_files: number
    /** 每次模型调用按任务分目录存进 logs/prompt/<任务>/，密钥已隐去 */
    prompt_records: boolean
    /** 每个任务子目录保留的记录份数 */
    max_prompt_records_per_task: number
    event_retention_count: number; event_retention_hours: number
  }
  advanced: {
    https_proxy: string
  }
}

/** 设置窗口的 bridge：首次启动引导和后续编辑共用同一套。 */
export interface SettingsBridge {
  /** 读取当前配置；配置目录不存在时返回 Python 侧 Config() 对应的默认值。 */
  read(): Promise<YueliConfig>
  save(config: YueliConfig): Promise<{ ok: boolean; error?: string }>
  /** 重启 Python 后端，让刚保存的配置生效。 */
  restartBackend(): void
}

declare global {
  interface Window {
    pet: PetBridge
    diary?: DiaryBridge
    settings?: SettingsBridge
  }
}
