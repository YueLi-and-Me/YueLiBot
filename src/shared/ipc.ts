/**
 * 主进程 ↔ 渲染进程的通道名。两端共用，避免字符串写错却没人发现。
 *
 * NOTE: src/core/ と src/main/observability.ts を削除したため、
 * ParseEvent / DayPlan / ObservabilityPayload はここでインライン定義する。
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

/** 观察面板数据现在由 Python 组装，结构随 Python 侧变化而变化，渲染层按 key 读取。 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type ObservabilityPayload = any
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
  /** 设置窗口 → 主进程：读取当前 config.toml（不存在则返回默认值）。 */
  ReadConfig: 'settings:read-config',
  /** 设置窗口 → 主进程：写 config.toml；首次启动时这个调用成功后才会继续正常启动流程。 */
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
  readTrace(since?: number): Promise<unknown[]>
  /** 打开设置窗口。 */
  openSettings(): void
}

/**
 * config.toml 的形状，字段名和 TOML/Python 侧的 pydantic 模型一一对应
 * （snake_case，不转 camelCase——这份数据直接从表单写进 TOML，不经过任何
 * 面向渲染层的转译层，保持字段名一致能少一类"改了 Python 忘了改这里"的 bug）。
 */
export interface YueliConfig {
  bot: { user_nickname: string; relationship: string }
  llm: {
    provider: string; model: string; base_url: string; api_key: string
    thinking: 'disabled' | 'enabled' | 'auto'; timeout_ms: number
  }
  tts: {
    enabled: boolean; base_url: string; api_key: string; model: string; voice: string
    format: 'mp3' | 'wav' | 'opus'; speed: number
  }
  vision: {
    enabled: boolean; model: string; api_key: string; base_url: string
    folder_enabled: boolean; fullscreen_silent: boolean
  }
  vector: {
    enabled: boolean; embedding_base_url: string; embedding_api_key: string
    embedding_model: string; embedding_dim: number
  }
  advanced: { log_level: string; https_proxy: string }
}

/** 设置窗口的 bridge：首次启动引导和后续编辑共用同一套。 */
export interface SettingsBridge {
  /** 读取当前配置；文件不存在时返回全字段的默认值（对应 Python 侧 Config() 的默认值）。 */
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
