"""通过适配器 WebSocket 通道发送 QQ 出站消息。

主体进程不直接持有 QQ 协议连接，而是调用注入的 ``push`` 回调，将目标 stream
和已切分的消息交给适配器；适配器返回的订阅数量用于判断消息是否确实有接收端。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from src.core.platform_io.driver import DeliveryError, PlatformDriver
from src.core.platform_io.types import (
    DeliveryReceipt,
    OutboundMessage,
    OutboundPoke,
    OutboundReaction,
)


class QqWebSocketDriver(PlatformDriver):
    """把一轮完整回复推到适配器订阅的 QQ WebSocket 通道。"""

    platform = 'qq'

    def __init__(self, push: Callable[[int, str, Any], Awaitable[int]]) -> None:
        """初始化 QQ WebSocket 出站驱动。

        :param push: 异步消息分发回调，参数依次为 stream ID、事件名称和事件载荷，
                返回已接收事件的订阅者数量。
        """

        self._push = push

    async def start(self) -> None:
        """完成驱动启动。

        QQ 连接由独立适配器维护，因此该实现不额外创建资源。

        :return: ``None``。
        """

        return None

    async def stop(self) -> None:
        """完成驱动停止。

        该驱动不持有连接，适配器生命周期由调用方管理。

        :return: ``None``。
        """

        return None

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """将一轮 QQ 回复作为单个事件发送到适配器。

        :param message: 目标必须是 QQ 的 direct 或 group stream；``segments`` 会
                按原顺序复制到事件载荷。

        :return: 记录 QQ 平台和 stream ID 的投递回执；当前通道不提供外部消息编号。

        :raises DeliveryError: 目标平台或 stream 类型不匹配，或没有适配器订阅者。

        副作用：
            调用一次注入的 ``push`` 回调，可能通过 WebSocket 向适配器发送消息。
        """
        # 先校验平台和 stream 类型，再调用适配器，避免向错误目标发送协议事件。
        if message.stream.platform != self.platform:
            raise DeliveryError(
                f'QQ driver 收到非 QQ stream：{message.stream.platform}'
            )
        if message.stream.kind not in {'direct', 'group'}:
            raise DeliveryError(f'QQ driver 不支持 {message.stream.kind} stream')
        # 一轮回复封装为一个 qq.send 事件，保持分句顺序并避免客户端重复拼接。
        payload: dict[str, Any] = {
            'streamKind': message.stream.kind,
            'streamExternalId': message.stream.external_id,
            'segments': list(message.segments),
        }
        if message.batch_delays_ms:
            payload['batchDelaysMs'] = list(message.batch_delays_ms)
        if message.quote_external_message_id:
            payload['quoteExternalMessageId'] = message.quote_external_message_id
        if message.emoji_refs:
            payload['emojiRefs'] = list(message.emoji_refs)
            payload['emojiSubTypes'] = list(message.emoji_sub_types)
        if message.turn_id:
            # 只在有回合上下文时下发：缺省不发新字段，保持既有精确载荷断言不变。
            payload['turnId'] = message.turn_id
        delivered = await self._push(message.stream.id, 'qq.send', payload)
        if delivered == 0:
            # 没有订阅者时不能返回成功回执，否则上层会误以为消息已经送达。
            raise DeliveryError(
                f'QQ stream {message.stream.id} 没有适配器 WebSocket 订阅者'
            )
        return DeliveryReceipt(
            platform=self.platform,
            stream_id=message.stream.id,
            external_message_ids=[],
        )

    async def react(self, reaction: OutboundReaction) -> DeliveryReceipt:
        """把一次表情回应作为独立事件发送到适配器。

        走独立的 ``qq.react`` 通道而不是复用 ``qq.send``：协议端那边是另一个
        action（贴表情不发消息），共用通道会让适配器必须靠字段有无来猜自己该做
        什么，而那正是消息与回应被混淆的开始。

        :param reaction: 目标必须是 QQ stream；被回应消息的平台编号由核心解析。

        :return: 记录 QQ 平台和 stream ID 的投递回执；表情回应不产生新消息编号。

        :raises DeliveryError: 目标平台不匹配，或没有适配器订阅者。

        副作用：调用一次注入的 ``push`` 回调。
        """
        if reaction.stream.platform != self.platform:
            raise DeliveryError(
                f'QQ driver 收到非 QQ stream：{reaction.stream.platform}'
            )
        reaction_payload: dict[str, Any] = {
            'streamKind': reaction.stream.kind,
            'streamExternalId': reaction.stream.external_id,
            'targetExternalMessageId': reaction.target_external_message_id,
            'reaction': reaction.reaction,
        }
        if reaction.turn_id:
            reaction_payload['turnId'] = reaction.turn_id
        delivered = await self._push(reaction.stream.id, 'qq.react', reaction_payload)
        if delivered == 0:
            raise DeliveryError(
                f'QQ stream {reaction.stream.id} 没有适配器 WebSocket 订阅者'
            )
        return DeliveryReceipt(
            platform=self.platform,
            stream_id=reaction.stream.id,
            external_message_ids=[],
        )

    async def poke(self, poke: OutboundPoke) -> DeliveryReceipt:
        """把一次戳一戳作为独立事件发送到适配器。

        :param poke: 目标必须是 QQ stream；被戳者的 QQ 号由核心解析。

        :return: 记录 QQ 平台和 stream ID 的投递回执。

        :raises DeliveryError: 目标平台不匹配，或没有适配器订阅者。

        副作用：调用一次注入的 ``push`` 回调。
        """
        if poke.stream.platform != self.platform:
            raise DeliveryError(f'QQ driver 收到非 QQ stream：{poke.stream.platform}')
        poke_payload: dict[str, Any] = {
            'streamKind': poke.stream.kind,
            'streamExternalId': poke.stream.external_id,
            'targetExternalId': poke.target_external_id,
        }
        if poke.turn_id:
            poke_payload['turnId'] = poke.turn_id
        delivered = await self._push(poke.stream.id, 'qq.poke', poke_payload)
        if delivered == 0:
            raise DeliveryError(
                f'QQ stream {poke.stream.id} 没有适配器 WebSocket 订阅者'
            )
        return DeliveryReceipt(
            platform=self.platform,
            stream_id=poke.stream.id,
            external_message_ids=[],
        )
