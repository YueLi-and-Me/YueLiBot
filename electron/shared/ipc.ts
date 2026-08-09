/**
 * 主进程 ↔ 渲染进程的通道名。两端共用，避免字符串写错却没人发现。
 *
 * 注：src/core/ 与 src/main/observability.ts 已删除，
 * ParseEvent / DayPlan / ObservabilityPayload 因此就地定义在本文件。
 */

/** 流式响应解析事件（Python 侧通过 WS 推来，主进程原样转发）。 */
export type ParseEvent =
  | { type: 'say'; emotion?: string; gesture?: string }
  | { type: 'text'; value: string }
  | { type: 'sayEnd' }
  | { type: 'memory'; memoryType?: string; content: string }
  | { type: 'mood'; favor?: number; energy?: number }

/** 日程时段（Python 侧 DayPlan.slots 字段，渲染层只读取）。 */
export interface DayPlanSlot { from: string; doing: string; mood: string }

/** 每日计划（Python 侧产物，通过 /diary 下发）。 */
export interface DayPlan {
  date: string
  slots: DayPlanSlot[]
  bedtimeHint: string
  wakeHint: string
  theme: string
  carryOver: string
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

/** 人格关系轴与全局精力。 */
export interface ObservabilityPersonaState {
  intimacy: number
  tsundere: number
  reliance: number
  energy: number
  updated_at: number
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

/** Python `/observability` 的完整跨语言契约。 */
export interface ObservabilityPayload {
  now: number
  persona: {
    state: ObservabilityPersonaState
    description: string
  }
  schedule: DayPlan | null
  memory: {
    semantic: ObservabilityFact[]
    episodes: number
    workingMessages: number
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

/** `/debug/trace` 的稳定公共字段；各 kind 的业务字段保留为 unknown。 */
export interface TraceEntry {
  seq: number
  at: number
  kind: string
  streamId?: number
  platform?: string
  personId?: number
  personKind?: string
  turnId?: number
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
  /** 渲染层 → 主进程：要打字了，把焦点拿过来 */
  FocusInput: 'pet:focus-input',
  /** 渲染层 → 主进程：用户点了角色，需要暂时保持醒着。 */
  UserInteracted: 'pet:user-interacted',

  /** 渲染层 → 主进程：发一句话。返回本轮的 turnId。 */
  Send: 'chat:send',
  /** 渲染层 → 主进程：打断她正在说的话 */
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
  /** 观察窗口 → 主进程：取得内部状态快照，只读。 */
  Observability: 'observability:get',
  /** 观察窗口 → 主进程：增量拉取运行时调试追踪（用户输入/LLM 请求/流式增量/最终响应等）。 */
  DebugTrace: 'debug:trace',
  /** 设置窗口 → 主进程：读取拆分后的配置目录（不存在则返回默认值）。 */
  ReadConfig: 'settings:read-config',
  /** 设置窗口 → 主进程：写入拆分配置；首次启动时这个调用成功后才会继续正常启动流程。 */
  SaveConfig: 'settings:save-config',
  /** 设置窗口 → 主进程：重启 Python 后端，使刚保存的配置生效。 */
  RestartBackend: 'settings:restart-backend',
  /** 观察窗口 → 主进程：打开设置窗口。 */
  OpenSettings: 'settings:open',
} as const

/**
 * 推给渲染层的对话事件。
 *
 * 带 turnId 是因为用户可能在她说话时打断并重新发问 ——
 * 没有轮次标记的话，上一轮的残余 token 会串进新气泡。
 */
export type ChatStreamEvent =
  | { turnId: number; kind: 'parse'; event: ParseEvent }
  | { turnId: number; kind: 'done' }
  | { turnId: number; kind: 'error'; message: string; hint?: string }

/**
 * 语音事件。音频走 base64 而不是 ArrayBuffer —— Electron 的结构化克隆
 * 对 Buffer 的处理在各版本间有差异，base64 是最省心的跨进程形态。
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
  /** 「什么情景下会想起这段」—— 她记这件事的理由。 */
  cues: string[]
}

/** 日记内容与主进程统一业务时钟一起下发，渲染层不应自行读取 Date.now()。 */
export interface DiaryPayload {
  entries: DiaryEntry[]
  /** 她当天的生活计划；日记以第一人称呈现，不显示人格数值。 */
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
  send(text: string): Promise<number>
  interrupt(): void
  onEvent(handler: (e: ChatStreamEvent) => void): () => void
  /** 主进程要求打开输入栏（托盘「跟她说话」）。 */
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

/** 开发者观察窗口的最小只读 bridge。 */
export interface ObservabilityBridge {
  read(): Promise<ObservabilityPayload>
  /** 增量拉取调试追踪；since 传上次拿到的最大 seq，默认从头。 */
  readTrace(since?: number): Promise<TraceEntry[]>
  /** 打开设置窗口。 */
  openSettings(): void
}

/** 请求协议适配器：openai = OpenAI 兼容；volcengine = 豆包语音私有协议，只能用于 tts。 */
export type ClientType = 'openai' | 'volcengine'

/**
 * 任务在多个候选模型之间的挑选顺序。
 *   sequential = 按列表顺序，永远优先第一条（主备）
 *   random     = 每次随机起点，把流量摊到多家（分摊额度）
 * 两种策略都遵守同一条熔断规则：刚失败过的厂商在冷却期内排到最后。
 */
export type SelectionStrategy = 'sequential' | 'random'

/** providers.toml 里的一条可复用连接。一个厂商可供多个模型引用。 */
export interface ApiProviderConfig {
  /** 配置内部引用名，必须唯一；模型的 api_provider 写这个值 */
  name: string
  /** 厂商预设名，base_url 留空时用于选择内置官方地址 */
  kind: string
  base_url: string
  api_key: string
  client_type: ClientType
  /** 仅 volcengine：App ID，与 api_key（Access Token）成对使用 */
  app_id: string
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
  thinking: 'disabled' | 'enabled' | 'auto'
  /** 向量维度，仅 embedding 模型使用 */
  embedding_dim: number
}

/** 一个任务的候选模型列表与轮询策略。列表里排第一的是主力。 */
export interface TaskRoutingConfig {
  model_list: string[]
  selection_strategy: SelectionStrategy
}

/**
 * 设置页使用的任务视图。持久化层会把它拆成 providers/models/bot/features
 * 四份 TOML，Python 侧再组合成同样的运行时结构。
 */
export interface YueliConfig {
  bot: { name: string; user_nickname: string; relationship: string }
  personality: {
    identity: string
    behavior: string
    reply_style: string
    attention: string
    boundaries: string
    tone_probability: number
    tone_variants: string[]
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
  }
  generation: {
    chat: { temperature: number; max_tokens: number }
    proactive: { enabled: boolean; temperature: number; max_tokens: number }
    summary: { temperature: number; max_tokens: number }
    schedule: { temperature: number; max_tokens: number }
    vision: { temperature: number; max_tokens: number }
  }
  /** 所有可用连接。轮询就是在这些连接之间换。 */
  api_providers: ApiProviderConfig[]
  /** 所有模型定义。同一个厂商可以有多个模型，同一个模型 ID 也能挂在多个厂商下。 */
  models: ModelDefinitionConfig[]
  /** 四类任务各自的候选模型与轮询策略。 */
  model_tasks: {
    chat: TaskRoutingConfig
    vision: TaskRoutingConfig
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
    fullscreen_silent: boolean
    /** window = 只截前台那一个窗口；screen = 截整个主屏（能看到桌面，但会连带截到别的窗口） */
    capture_mode: 'window' | 'screen'
  }
  vector: {
    enabled: boolean
  }
  advanced: {
    log_level: string; https_proxy: string
    trace_max_bytes: number
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
    observability?: ObservabilityBridge
    settings?: SettingsBridge
  }
}
