"""非桌面平台的单播出站路由。

desktop 是零号出口，继续使用 ChatService 的解析事件链路，不实现 PlatformDriver。
原因不是流式内容无法落进 send()，而是 desktop 的五类解析事件中只有 TextEvent 和
SayEndEvent 有平台侧对应物；SayEvent 是立绘表情与动作，MemoryEvent 和 MoodEvent 是
观察面板副作用，并非待发送的消息。为一条唯一的 desktop 链路伪造驱动契约只会混淆边界。
"""

from __future__ import annotations

from typing import Dict

from src.platform_io.driver import DeliveryError, PlatformDriver
from src.platform_io.types import DeliveryReceipt, OutboundMessage


class PlatformBroker:
    """按 stream 单播到已注册的非桌面 driver。"""

    def __init__(self) -> None:
        self._drivers: Dict[int, PlatformDriver] = {}

    def register(self, stream_id: int, driver: PlatformDriver) -> None:
        """注册一个 stream 的唯一出站 driver，重复注册直接暴露配置错误。"""
        if stream_id in self._drivers:
            raise ValueError(f'stream {stream_id} 已注册出站 driver')
        self._drivers[stream_id] = driver

    def has_driver(self, stream_id: int) -> bool:
        """判断 stream 是否已经完成出站装配。"""
        return stream_id in self._drivers

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        """单播消息；目标 stream 未注册时不得静默丢弃。"""
        driver = self._drivers.get(message.stream.id)
        if driver is None:
            raise DeliveryError(f'stream {message.stream.id} 没有已注册的出站 driver')
        return await driver.send(message)
