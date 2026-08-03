"""
Pydantic 配置模型 —— 分区对应 config.toml 的
[bot] / [llm] / [tts] / [vision] / [vector] / [advanced]。

启动时一次性校验，字段缺失/类型错在进入任何业务逻辑之前就报错。
每个字段在 config.toml 里的注释由 Electron 设置窗口的写入模板负责
（见 src/renderer/settings.ts），这里的注释只是给读代码的人看。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class BotConfig(BaseModel):
    # 月璃眼中用户的名字/称呼，留空则不特别用名字称呼他
    user_nickname: str = ''
    # 月璃和用户的关系：哥哥/姐姐/朋友/自定义文本，留空则不设定这层关系
    relationship: str = ''


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
    timeout_ms: int = 120_000

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

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.base_url and self.model and self.voice)


class VisionConfig(BaseModel):
    # ⚠ 全项目唯一会把屏幕内容送上云的功能，开启前想清楚：
    #   只截前台那一个窗口、绝不落盘、缩到 768px 宽再传，但截图里仍可能有
    #   明文密码、私信、银行页面、公司文档
    enabled: bool = False
    # 留空则复用对话模型（仅当对话接口本身接受 image_url 时可用）
    model: str = ''
    api_key: str = ''
    base_url: str = ''
    # 资源管理器截图常带路径、文档名和下载记录，隐私风险更高，默认关
    folder_enabled: bool = False
    # 疑似全屏时静默，避免直播/录屏把桌宠声音带进去
    fullscreen_silent: bool = True

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


class Config(BaseModel):
    bot: BotConfig = Field(default_factory=BotConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    advanced: AdvancedConfig = Field(default_factory=AdvancedConfig)
