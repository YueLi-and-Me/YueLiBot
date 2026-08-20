"""非桌面平台的单播出站路由。

desktop 继续使用 ChatService 的解析事件链路，不实现 PlatformDriver。解析事件中只有
TextEvent 和 SayEndEvent 对应平台消息，SayEvent、MemoryEvent 与 MoodEvent 分别负责
表现层和观察层副作用，因此本模块只为 direct 与 group 等外部平台提供单播驱动路由。
"""

from __future__ import annotations

from typing import Dict

from src.core.platform_io.driver import DeliveryError, PlatformDriver
from src.core.platform_io.types import (
    DeliveryReceipt,
    OutboundMessage,
    OutboundPoke,
    OutboundReaction,
)


class PlatformBroker:
    """按 stream 单播到已注册的非桌面 driver。"""

    def __init__(self) -> None:
        """创建空的 stream 到出站驱动映射。

        映射按 stream 唯一约束，重复注册和未注册投递都会显式报错。
        """

        self._drivers: Dict[int, PlatformDriver] = {}

    def register(self, stream_id: int, driver: PlatformDriver) -> None:
        """为一个会话注册唯一的出站驱动。

        :param stream_id: ``streams.id`` 稳定主键。
        :param driver: 负责该会话投递的异步平台驱动。

        :raises ValueError: 该会话已经注册驱动。

        副作用：
            修改内存路由表；不会启动驱动或发送消息。
        """
        if stream_id in self._drivers:
            raise ValueError(f'stream {stream_id} 已注册出站 driver')
        self._drivers[stream_id] = driver

    def has_driver(self, stream_id: int) -> bool:
        """判断指定会话是否已经完成出站驱动装配。

        :param stream_id: ``streams.id`` 稳定主键。

        :return: 已注册时返回 ``True``，否则返回 ``False``。
        """
        return stream_id in self._drivers

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        """将一条出站消息单播到其所属会话的驱动。

        :param message: 已按句切分并带有目标 stream 的出站消息。

        :return: 目标驱动返回的投递回执。

        :raises DeliveryError: 目标 stream 尚未注册出站驱动，或驱动报告投递失败。
        """
        driver = self._drivers.get(message.stream.id)
        if driver is None:
            raise DeliveryError(f'stream {message.stream.id} 没有已注册的出站 driver')
        return await driver.send(message)

    async def dispatch_reaction(self, reaction: OutboundReaction) -> DeliveryReceipt:
        """把一次表情回应单播到其所属会话的驱动。

        :param reaction: 已确定目标消息平台编号与语义反应标识的表情回应。

        :return: 目标驱动返回的投递回执。

        :raises DeliveryError: 目标 stream 尚未注册出站驱动，驱动不支持表情
            回应，或平台报告失败。
        """
        driver = self._drivers.get(reaction.stream.id)
        if driver is None:
            raise DeliveryError(f'stream {reaction.stream.id} 没有已注册的出站 driver')
        return await driver.react(reaction)

    async def dispatch_poke(self, poke: OutboundPoke) -> DeliveryReceipt:
        """把一次戳一戳单播到其所属会话的驱动。

        :param poke: 已确定被戳者平台标识的戳一戳。

        :return: 目标驱动返回的投递回执。

        :raises DeliveryError: 目标 stream 尚未注册出站驱动，驱动不支持戳一戳，
            或平台报告失败。
        """
        driver = self._drivers.get(poke.stream.id)
        if driver is None:
            raise DeliveryError(f'stream {poke.stream.id} 没有已注册的出站 driver')
        return await driver.poke(poke)
