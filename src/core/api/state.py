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
        副作用：初始化所有服务引用为空；群聊配置留空，等待启动流程显式写入。
        """
        self.chat: Any = None          # ChatService
        self.awareness: Any = None     # AwarenessService
        self.vector: Any = None        # VectorService（导入中心为新知识补向量）
        self.emoji_library: Any = None # EmojiLibrary（管理页读写与缩略图来源）
        self.tts: Any = None           # TtsService
        self.routers: Any = None       # ModelRouters（八个任务的候选与熔断状态）
        self.registry: Any = None       # StreamRegistry（stream/person/identity 的唯一入口）
        # 不默认构造 GroupChatConfig：漏赋值必须在入站调用点暴露，而不是安静地用默认窗口跑。
        self.group_chat_config: GroupChatConfig | None = None
        self.foreground_callback: Callable[[dict], None] | None = None
        self.broker: Any = None         # PlatformBroker（非桌面唯一出站接缝）
        self.register_platform_stream: Callable[[StreamRef], None] | None = None
        self.config_dir: Any = None               # 运行时配置目录，模型工作台读写 TOML 用
        # uvicorn.Server 句柄，由 main.py 在构造后注入；优雅关机端点据此置位 should_exit。
        self.uvicorn_server: Any = None


app_state = _AppState()
