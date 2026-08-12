"""保存 API 路由访问的全局服务引用和平台回调。

`_AppState` 只负责依赖注入容器，不创建具体服务；启动流程将 ChatService、
AwarenessService、注册表、平台 broker 等实例写入 `app_state`，HTTP/WS 路由随后读取。
"""

from __future__ import annotations

from typing import Any, Callable

from src.core.config.schema import GroupChatConfig
from src.core.platform_io.types import StreamRef


class _AppState:
    """集中保存运行时服务和跨层回调的可变状态容器。

    属性类型保持宽松是因为服务按启动顺序注入，路由在使用前会检查可选引用。
    """

    def __init__(self) -> None:
        """创建未启动服务的默认状态。

        :return: 无返回值。
        :side_effects: 初始化所有服务引用为空，并创建默认群聊配置。
        """
        self.chat: Any = None          # ChatService
        self.awareness: Any = None     # AwarenessService
        self.tts: Any = None           # TtsService
        self.routers: Any = None       # ModelRouters（八个任务的候选与熔断状态）
        self.registry: Any = None       # StreamRegistry（stream/person/identity 的唯一入口）
        self.group_chat_config = GroupChatConfig()
        self.foreground_callback: Callable[[dict], None] | None = None
        self.broker: Any = None         # PlatformBroker（非桌面唯一出站接缝）
        self.register_platform_stream: Callable[[StreamRef], None] | None = None


app_state = _AppState()
