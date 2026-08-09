"""主体进程侧的 QQ 出站 driver。"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from src.platform_io.driver import DeliveryError, PlatformDriver
from src.platform_io.types import DeliveryReceipt, OutboundMessage


class QqWebSocketDriver(PlatformDriver):
    """把一轮完整回复推到适配器订阅的 QQ WebSocket 通道。"""

    platform = 'qq'

    def __init__(self, push: Callable[[int, str, Any], Awaitable[int]]) -> None:
        self._push = push

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """整轮只投递一次；没有适配器订阅者就明确报告故障。"""
        if message.stream.platform != self.platform:
            raise DeliveryError(
                f'QQ driver 收到非 QQ stream：{message.stream.platform}'
            )
        if message.stream.kind not in {'direct', 'group'}:
            raise DeliveryError(f'QQ driver 不支持 {message.stream.kind} stream')
        delivered = await self._push(
            message.stream.id,
            'qq.send',
            {
                'streamKind': message.stream.kind,
                'streamExternalId': message.stream.external_id,
                'segments': list(message.segments),
            },
        )
        if delivered == 0:
            raise DeliveryError(
                f'QQ stream {message.stream.id} 没有适配器 WebSocket 订阅者'
            )
        return DeliveryReceipt(
            platform=self.platform,
            stream_id=message.stream.id,
            external_message_ids=[],
        )
