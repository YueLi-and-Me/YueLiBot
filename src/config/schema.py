"""
Pydantic 配置模型。

持久化层由 providers/models/bot/features 四份 TOML 组成；加载器会把它们
组合为本文件末尾的 Config 运行时视图，业务服务不需要知道磁盘布局。

启动时一次性校验，字段缺失/类型错在进入任何业务逻辑之前就报错。
每个字段在 TOML 里的注释由 Electron 主进程的写入模板负责
（见 src/main/config.ts），这里的注释只是给读代码的人看。
"""

from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.agent.character import (
    ATTENTION_PROMPT,
    BEHAVIOR_PROMPT,
    BOUNDARIES_PROMPT,
    CHARACTER_NAME,
    IDENTITY_PROMPT,
    REPLY_STYLE_PROMPT,
    TONE_PROBABILITY,
    TONE_VARIANTS,
)


class InnerConfig(BaseModel):
    # 1.1.0 起 model_tasks 从「一个任务一个模型名」改成候选列表 + 轮询策略。
    # 旧配置由 Electron 侧在读取时就地升级，Python 只解析当前版本。
    version: Literal['1.1.0'] = '1.1.0'


class BotConfig(BaseModel):
    # Bot 的显示名和提示词身份名
    name: str = CHARACTER_NAME
    # 月璃眼中用户的名字/称呼，留空则不特别用名字称呼他
    user_nickname: str = ''
    # 月璃和用户的关系：哥哥/姐姐/朋友/自定义文本，留空则不设定这层关系
    relationship: str = ''


class PersonalityConfig(BaseModel):
    """可以由 bot.toml 完整改写的人格提示词，不再硬编码在 ChatService。"""

    identity: str = IDENTITY_PROMPT
    behavior: str = BEHAVIOR_PROMPT
    reply_style: str = REPLY_STYLE_PROMPT
    attention: str = ATTENTION_PROMPT
    boundaries: str = BOUNDARIES_PROMPT
    tone_probability: float = Field(default=TONE_PROBABILITY, ge=0.0, le=1.0)
    tone_variants: List[str] = Field(default_factory=lambda: list(TONE_VARIANTS))


class ConversationConfig(BaseModel):
    """对话窗口、摘要节奏和记忆召回数量。"""

    working_memory_messages: int = Field(default=40, ge=8, le=200)
    summarize_trigger_messages: int = Field(default=48, ge=12, le=500)
    summarize_batch_messages: int = Field(default=16, ge=4, le=200)
    session_gap_minutes: int = Field(default=30, ge=1, le=1440)
    fact_recall_limit: int = Field(default=6, ge=0, le=50)
    recalled_episode_limit: int = Field(default=2, ge=0, le=20)
    recent_episode_limit: int = Field(default=2, ge=0, le=20)
    episode_context_limit: int = Field(default=3, ge=0, le=30)

    @model_validator(mode='after')
    def _validate_summary_window(self) -> 'ConversationConfig':
        if self.summarize_batch_messages >= self.summarize_trigger_messages:
            raise ValueError('summarize_batch_messages 必须小于 summarize_trigger_messages')
        remaining = self.summarize_trigger_messages - self.summarize_batch_messages
        if remaining > self.working_memory_messages:
            raise ValueError(
                'summarize_trigger_messages - summarize_batch_messages '
                '不能大于 working_memory_messages'
            )
        return self


class GenerationTaskConfig(BaseModel):
    """单类模型调用的采样参数；max_tokens=0 表示交给厂商决定。"""

    temperature: float = Field(default=0.85, ge=0.0, le=2.0)
    max_tokens: int = Field(default=0, ge=0, le=1_000_000)

    @property
    def token_limit(self) -> int | None:
        return self.max_tokens or None


class ProactiveGenerationTaskConfig(GenerationTaskConfig):
    """主动系统的采样参数和总开关。关闭时 Electron 不安装全局键鼠钩子。"""

    enabled: bool = True


class GenerationConfig(BaseModel):
    """按任务拆分参数，避免改视觉模型时意外改变普通聊天。"""

    chat: GenerationTaskConfig = Field(default_factory=GenerationTaskConfig)
    proactive: ProactiveGenerationTaskConfig = Field(
        default_factory=lambda: ProactiveGenerationTaskConfig(temperature=0.9, max_tokens=200)
    )
    summary: GenerationTaskConfig = Field(
        default_factory=lambda: GenerationTaskConfig(temperature=0.3, max_tokens=0)
    )
    schedule: GenerationTaskConfig = Field(
        default_factory=lambda: GenerationTaskConfig(temperature=0.95, max_tokens=700)
    )
    vision: GenerationTaskConfig = Field(
        default_factory=lambda: GenerationTaskConfig(temperature=0.3, max_tokens=120)
    )


class TtsConfig(BaseModel):
    """语音合成的功能参数。地址、密钥、协议都在它的候选模型里，不在这里。"""

    # 不开就是纯文字，其余功能不受影响
    enabled: bool = False
    voice: str = ''
    format: Literal['mp3', 'wav', 'opus'] = 'mp3'
    # 陪伴场景略慢一点更自然，太快像播报
    speed: float = Field(default=0.95, ge=0.25, le=4.0)
    # 仅 volcengine 协议使用：豆包语音的集群名
    cluster: str = 'volcano_tts'


class VisionConfig(BaseModel):
    # ⚠ 除非 base_url 指向本机，否则这是全项目唯一会把屏幕内容送出去的功能。
    #   开启前想清楚：绝不落盘、缩到 768px 宽再传，但截图里仍可能有明文密码、
    #   私信、银行页面、公司文档。capture_mode = 'screen' 时风险显著更高，
    #   见下面那个字段的说明。
    #   base_url 填本地推理服务（如 Ollama 的 http://127.0.0.1:11434/v1）时，
    #   画面不出本机，上面这条顾虑不成立。
    enabled: bool = False
    # 疑似全屏时静默，避免直播/录屏把桌宠声音带进去
    fullscreen_silent: bool = True
    # 截什么：
    #   'window' = 只截前台那一个窗口（默认）。她看不到「桌面上有什么」，
    #              因为桌面本身、其它窗口都不在画面里。
    #   'screen' = 截整个主屏。她能看到桌面全貌，代价是会连带截到第二个窗口、
    #              后台的聊天窗、没关的网页——凡是当时屏幕上有的都会被送走。
    #              指向云端模型时尤其要想清楚。
    capture_mode: Literal['window', 'screen'] = 'window'

    @property
    def ready(self) -> bool:
        return self.enabled


class VectorConfig(BaseModel):
    # 向量混合召回，默认关；还需 pip install yueli[vector]
    # 用哪个 embedding 模型由 model_tasks.embedding 决定，不在这里重复。
    enabled: bool = False


class AdvancedConfig(BaseModel):
    log_level: str = 'INFO'
    # 全局 HTTP(S) 代理，例如 http://127.0.0.1:7890
    https_proxy: str = ''
    # ★ 默认关闭：开启后 trace.jsonl 会明文记录用户输入、完整系统提示词
    #   （含召回的事实与情节）、整个工作记忆窗口和模型输出。排查问题时再开。
    trace_content: bool = False
    # trace.jsonl 单文件上限，超过就轮转成 .1 并重开
    trace_max_bytes: int = 8 * 1024 * 1024


class ApiProviderConfig(BaseModel):
    """providers.toml 中的一条可复用连接定义。"""

    name: str
    kind: str
    base_url: str = ''
    api_key: str = ''
    # openai = OpenAI 兼容协议（对话/视觉/向量/TTS 都走它）
    # volcengine = 豆包语音私有协议，只能用于 tts 任务
    client_type: Literal['openai', 'volcengine'] = 'openai'
    # 豆包语音要 App ID + Access Token 两个凭证，api_key 放 Access Token
    app_id: str = ''
    timeout_ms: int = Field(default=120_000, ge=1_000, le=3_600_000)
    max_retries: int = Field(default=2, ge=0, le=10)
    retry_interval_ms: int = Field(default=800, ge=0, le=60_000)


class ProviderCatalog(BaseModel):
    inner: InnerConfig
    api_providers: List[ApiProviderConfig]


class ModelDefinitionConfig(BaseModel):
    """models.toml 中的具体模型，借助 api_provider 引用厂商连接。"""

    name: str
    model_identifier: str = ''
    api_provider: str
    thinking: Literal['disabled', 'enabled', 'auto'] = 'disabled'
    embedding_dim: int = 0


class TaskRoutingConfig(BaseModel):
    """一个任务的候选模型与轮询策略。

    model_list 排第一的是主力，其余是它挂掉之后依次顶上的备用。
    sequential = 永远优先第一条；random = 每次随机起点，把流量摊到多家。
    """

    model_config = ConfigDict(protected_namespaces=())

    model_list: List[str] = Field(default_factory=list)
    selection_strategy: Literal['sequential', 'random'] = 'sequential'

    @field_validator('model_list')
    @classmethod
    def _reject_duplicates(cls, v: List[str]) -> List[str]:
        # 同一个模型写两遍只会让轮询白撞一次，属于明显的手误。
        if len(set(v)) != len(v):
            raise ValueError(f'model_list 存在重复模型：{v}')
        return v


class ModelTaskConfig(BaseModel):
    # 段名写错必须炸在加载期。留空继承 chat 是合法语义，段名打错不是——
    # 没有这一条，[model_tasks.summry] 会静默变成「跟 chat 一样」。
    model_config = ConfigDict(extra='forbid')

    chat: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    proactive: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    summary: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    schedule: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    vision: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    tts: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    embedding: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)


class ModelCandidate(BaseModel):
    """把「模型定义 + 它引用的厂商连接」合成一条，是轮询的最小单位。

    运行时视图：由 loader 组装，不直接对应任何一段 TOML。
    """

    # 配置内部模型名，出现在日志里，方便对照 models.toml
    name: str
    # 引用的厂商名。熔断按厂商记——一个厂商挂了，它下面所有模型都别再撞。
    provider: str
    kind: str = ''
    base_url: str = ''
    api_key: str = ''
    # 发给厂商接口的真实模型 ID
    identifier: str = ''
    thinking: Literal['disabled', 'enabled', 'auto'] = 'disabled'
    client_type: Literal['openai', 'volcengine'] = 'openai'
    app_id: str = ''
    embedding_dim: int = 0
    timeout_ms: int = Field(default=120_000, ge=1_000, le=3_600_000)
    max_retries: int = Field(default=0, ge=0, le=10)
    retry_interval_ms: int = Field(default=0, ge=0, le=60_000)


class TaskRouting(BaseModel):
    """一个任务解析完成后的候选序列。candidates 为空表示这个任务没有模型。"""

    task: str
    candidates: List[ModelCandidate] = Field(default_factory=list)
    strategy: Literal['sequential', 'random'] = 'sequential'

    @property
    def ready(self) -> bool:
        return bool(self.candidates)


class RoutingConfig(BaseModel):
    """七类任务各自的候选序列。业务侧只跟这里打交道，不再关心厂商怎么配。"""

    chat: TaskRouting = Field(default_factory=lambda: TaskRouting(task='chat'))
    proactive: TaskRouting = Field(default_factory=lambda: TaskRouting(task='proactive'))
    summary: TaskRouting = Field(default_factory=lambda: TaskRouting(task='summary'))
    schedule: TaskRouting = Field(default_factory=lambda: TaskRouting(task='schedule'))
    vision: TaskRouting = Field(default_factory=lambda: TaskRouting(task='vision'))
    tts: TaskRouting = Field(default_factory=lambda: TaskRouting(task='tts'))
    embedding: TaskRouting = Field(default_factory=lambda: TaskRouting(task='embedding'))


class ModelCatalog(BaseModel):
    inner: InnerConfig
    model_tasks: ModelTaskConfig
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    models: List[ModelDefinitionConfig]


class BotDocument(BaseModel):
    inner: InnerConfig
    bot: BotConfig
    personality: PersonalityConfig
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)


class FeatureDocument(BaseModel):
    inner: InnerConfig
    tts: TtsConfig
    vision: VisionConfig
    vector: VectorConfig
    advanced: AdvancedConfig


class Config(BaseModel):
    bot: BotConfig = Field(default_factory=BotConfig)
    personality: PersonalityConfig = Field(default_factory=PersonalityConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    # 七类任务的候选模型与轮询策略；连接细节都收在候选里
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    advanced: AdvancedConfig = Field(default_factory=AdvancedConfig)
