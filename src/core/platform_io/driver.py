"""定义非桌面平台出站驱动的生命周期与投递契约。

``PlatformBroker`` 通过本模块的抽象接口管理平台驱动，具体协议实现位于
``src.core.platform_io.drivers``。驱动接收已经按句切分的消息，并返回可追踪的
``DeliveryReceipt``；连接管理和平台错误由具体实现负责。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.core.platform_io.types import (
    DeliveryReceipt,
    OutboundMessage,
    OutboundPoke,
    OutboundReaction,
)


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

    async def react(self, reaction: OutboundReaction) -> DeliveryReceipt:
        """给一条已有消息贴上表情回应。

        默认实现直接拒绝：表情回应是可选平台能力，多数通道没有对应协议动作。
        **这不是兜底**——核心只在 ``PlatformCapabilities.react`` 为真时才会走到
        这里，而那个开关本身是按平台算出来的；真的走到这里说明能力判定与驱动
        实现已经不一致，必须当场报错而不是静默吞掉一次 Bot 做出的动作。

        :param reaction: 目标会话、被回应消息的平台编号与语义反应标识。

        :return: 记录平台与 stream 的投递回执；表情回应不产生新消息编号。

        :raises DeliveryError: 平台不支持表情回应，或调用被平台拒绝。
        """
        raise DeliveryError(
            f'{self.platform} driver 不支持表情回应，但核心把它当成了可用能力'
        )

    async def poke(self, poke: OutboundPoke) -> DeliveryReceipt:
        """戳一戳目标会话里的某个人。

        与 :meth:`react` 同款：默认实现直接拒绝，因为核心只在
        ``PlatformCapabilities.poke`` 为真时才会走到这里，真的走到了说明能力
        判定与驱动实现已经不一致，必须当场报错而不是静默吞掉一次 Bot 做出的动作。

        :param poke: 目标会话与被戳者的平台标识。

        :return: 记录平台与 stream 的投递回执；戳一戳不产生消息编号。

        :raises DeliveryError: 平台不支持戳一戳，或调用被平台拒绝。
        """
        raise DeliveryError(
            f'{self.platform} driver 不支持戳一戳，但核心把它当成了可用能力'
        )
