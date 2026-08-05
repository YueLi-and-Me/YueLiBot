"""非桌面平台的出站驱动契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.platform_io.types import DeliveryReceipt, OutboundMessage


class DeliveryError(RuntimeError):
    """消息无法投递到已注册的平台时抛出。"""


class PlatformDriver(ABC):
    """一个非桌面平台的出站投递实现。"""

    platform: str

    @abstractmethod
    async def start(self) -> None:
        """启动驱动持有的连接或后台资源。"""

    @abstractmethod
    async def stop(self) -> None:
        """停止驱动持有的连接或后台资源。"""

    @abstractmethod
    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """投递一条已经按句切分的消息。"""
