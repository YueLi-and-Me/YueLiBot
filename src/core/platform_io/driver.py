"""定义非桌面平台出站驱动的生命周期与投递契约。

``PlatformBroker`` 通过本模块的抽象接口管理平台驱动，具体协议实现位于
``src.core.platform_io.drivers``。驱动接收已经按句切分的消息，并返回可追踪的
``DeliveryReceipt``；连接管理和平台错误由具体实现负责。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.core.platform_io.types import DeliveryReceipt, OutboundMessage


class DeliveryError(RuntimeError):
    """目标平台已注册但消息无法投递时抛出的领域错误。"""


class PlatformDriver(ABC):
    """一个非桌面平台的出站投递实现。"""

    platform: str

    @abstractmethod
    async def start(self) -> None:
        """启动驱动持有的连接或后台资源。

        :return: ``None``。调用完成后驱动应具备接收 ``send`` 请求的条件。

        :raises Exception: 连接建立或资源初始化失败时由具体实现抛出。
        """

    @abstractmethod
    async def stop(self) -> None:
        """停止驱动持有的连接或后台资源。

        :return: ``None``。

        :raises Exception: 资源关闭失败时由具体实现抛出。
        """

    @abstractmethod
    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """投递一条已经按句切分的消息。

        :param message: 包含目标 stream 和消息分句的出站消息。

        :return: 记录平台、stream 与外部消息编号的投递回执。

        :raises DeliveryError: 平台拒绝消息、连接不可用或投递未完成时抛出。
        """
