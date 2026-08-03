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

from pydantic import BaseModel, Field, field_validator, model_validator

from yueli.agent.character import (
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
    version: Literal['1.0.0'] = '1.0.0'


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


class GenerationConfig(BaseModel):
    """按任务拆分参数，避免改视觉模型时意外改变普通聊天。"""

    chat: GenerationTaskConfig = Field(default_factory=GenerationTaskConfig)
    proactive: GenerationTaskConfig = Field(
        default_factory=lambda: GenerationTaskConfig(temperature=0.9, max_tokens=200)
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


class LlmConfig(BaseModel):
    # 预设：ark | deepseek | dashscope | moonshot | openai | ollama
    provider: str = 'ark'
    # 模型 ID，留空则用预设的默认模型（部分厂商没有默认值，必须填）
    model: str = ''
    # 留空则用预设的官方地址
    base_url: str = ''
    api_key: str = ''
    # 深度思考：disabled（默认，推荐）| enabled | auto
    # ⚠ 开启后实测首字延迟从 3 秒涨到 26~31 秒，桌宠场景里这等于产品报废
    thinking: Literal['disabled', 'enabled', 'auto'] = 'disabled'
    timeout_ms: int = Field(default=120_000, ge=1_000, le=3_600_000)
    # 仅在还没有输出任何内容时重试网络错误、HTTP 429 和 5xx，避免重复文本。
    max_retries: int = Field(default=2, ge=0, le=10)
    retry_interval_ms: int = Field(default=800, ge=0, le=60_000)

    @field_validator('provider')
    @classmethod
    def _normalize_provider(cls, v: str) -> str:
        return v.strip().lower()


class TtsConfig(BaseModel):
    # 不开就是纯文字，其余功能不受影响
    enabled: bool = False
    base_url: str = ''
    api_key: str = ''
    model: str = ''
    voice: str = ''
    format: Literal['mp3', 'wav', 'opus'] = 'mp3'
    # 陪伴场景略慢一点更自然，太快像播报
    speed: float = Field(default=0.95, ge=0.25, le=4.0)
    # 走哪套协议。openai = 兼容 /audio/speech；volcengine = 豆包语音。
    # 豆包语音官方不提供 OpenAI 兼容接口，认证是 App ID + Access Token，
    # 请求体也是另一套嵌套结构，所以只能单独走一条分支。
    client_type: Literal['openai', 'volcengine'] = 'openai'
    # ↓ 仅 volcengine 用：控制台「豆包语音 → 语音合成大模型 → 服务接口认证信息」
    app_id: str = ''
    cluster: str = 'volcano_tts'

    @property
    def ready(self) -> bool:
        if not self.enabled:
            return False
        if self.client_type == 'volcengine':
            # 豆包语音不吃 model，声音由 voice_type 决定；appid 和 token 缺一不可。
            return bool(self.app_id and self.api_key and self.voice)
        return bool(self.base_url and self.model and self.voice)


class VisionConfig(BaseModel):
    # ⚠ 除非 base_url 指向本机，否则这是全项目唯一会把屏幕内容送出去的功能。
    #   开启前想清楚：只截前台那一个窗口、绝不落盘、缩到 768px 宽再传，但截图
    #   里仍可能有明文密码、私信、银行页面、公司文档。
    #   base_url 填本地推理服务（如 Ollama 的 http://127.0.0.1:11434/v1）时，
    #   画面不出本机，上面这条顾虑不成立。
    enabled: bool = False
    # 留空则复用对话模型（仅当对话接口本身接受 image_url 时可用）
    model: str = ''
    api_key: str = ''
    base_url: str = ''
    # 这三个字段来自视觉模型所引用的 api_provider，不写在 features.toml。
    timeout_ms: int = Field(default=120_000, ge=1_000, le=3_600_000)
    max_retries: int = Field(default=2, ge=0, le=10)
    retry_interval_ms: int = Field(default=800, ge=0, le=60_000)
    # 一次送给模型几张关键帧。1 = 单张快照，模型拿不到任何帧间信息，
    # 只能描述「此刻是什么」；≥2 才谈得上「画面在发生什么变化」。
    # 云端按图计费，调大直接乘倍数；本地推理几乎无额外成本，建议 3。
    frames: int = Field(default=1, ge=1, le=4)
    # 资源管理器截图常带路径、文档名和下载记录，隐私风险更高，默认关
    folder_enabled: bool = False
    # 疑似全屏时静默，避免直播/录屏把桌宠声音带进去
    fullscreen_silent: bool = True

    @property
    def local(self) -> bool:
        """base_url 指向本机——用来决定要不要提示隐私风险。"""
        return any(host in self.base_url for host in ('127.0.0.1', 'localhost', '::1'))

    @property
    def ready(self) -> bool:
        return self.enabled


class VectorConfig(BaseModel):
    # 向量混合召回，默认关；还需 pip install yueli[vector]
    enabled: bool = False
    # 留空则复用 llm.base_url / llm.api_key
    embedding_base_url: str = ''
    embedding_api_key: str = ''
    embedding_model: str = 'text-embedding-3-small'
    embedding_dim: int = 1536


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


class ModelTaskConfig(BaseModel):
    chat: str
    vision: str
    tts: str
    embedding: str


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
    llm: LlmConfig = Field(default_factory=LlmConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    advanced: AdvancedConfig = Field(default_factory=AdvancedConfig)
