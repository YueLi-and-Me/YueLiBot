"""全局服务状态单例，供 http.py 等路由模块访问。"""

from __future__ import annotations

from typing import Any, Callable

from src.config.schema import GroupChatConfig
from src.platform_io.types import StreamRef


class _AppState:
    def __init__(self) -> None:
        self.chat: Any = None          # ChatService
        self.awareness: Any = None     # AwarenessService
        self.tts: Any = None           # TtsService
        self.routers: Any = None       # ModelRouters（四个任务的候选与熔断状态）
        self.registry: Any = None       # StreamRegistry（stream/person/identity 的唯一入口）
        self.group_chat_config = GroupChatConfig()
        self.foreground_callback: Callable[[dict], None] | None = None
        self.broker: Any = None         # PlatformBroker（非桌面唯一出站接缝）
        self.register_platform_stream: Callable[[StreamRef], None] | None = None


app_state = _AppState()
