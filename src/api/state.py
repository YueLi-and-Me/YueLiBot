"""全局服务状态单例，供 http.py 等路由模块访问。"""

from __future__ import annotations

from typing import Any, Callable


class _AppState:
    def __init__(self) -> None:
        self.chat: Any = None          # ChatService
        self.awareness: Any = None     # AwarenessService
        self.tts: Any = None           # TtsService
        self.routers: Any = None       # ModelRouters（四个任务的候选与熔断状态）
        self.foreground_callback: Callable[[dict], None] | None = None


app_state = _AppState()
