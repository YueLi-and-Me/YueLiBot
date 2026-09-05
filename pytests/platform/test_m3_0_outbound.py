"""验证平台出站分流及 QQ WebSocket 驱动的传输契约。

本模块覆盖桌面消息、QQ 私聊和群聊的目标解析、消息发送及投递回执，
依赖 PlatformBroker、PlatformDriver 和 QqWebSocketDriver 的实际接口。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from src.core.platform_io.broker import PlatformBroker
from src.core.platform_io.driver import DeliveryError
from src.core.platform_io.drivers.qq_ws import QqWebSocketDriver
from src.core.platform_io.types import InboundMessage, OutboundMessage, StreamKind, StreamRef
from src.core.config.schema import Config
from src.core.services.chat import ChatService


class _Provider:
    def __init__(self, response: str) -> None:
        self.response = response

    async def stream(self, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        yield {'text': self.response}


def _qq_stream(
    stream_id: int = 2,
    kind: StreamKind = 'direct',
    external_id: str = 'owner-qq',
) -> StreamRef:
    return StreamRef(
        id=stream_id,
        platform='qq',
        kind=kind,
        external_id=external_id,
    )


async def test_qq_driver_pushes_one_untagged_payload() -> None:
    calls: list[tuple[int, str, Any]] = []

    async def push(stream_id: int, channel: str, payload: Any) -> int:
        calls.append((stream_id, channel, payload))
        return 1

    driver = QqWebSocketDriver(push)
    receipt = await driver.send(OutboundMessage(
        stream=_qq_stream(),
        segments=['第一句', '第二句'],
    ))

    assert calls == [(
        2,
        'qq.send',
        {
            'streamKind': 'direct',
            'streamExternalId': 'owner-qq',
            'segments': ['第一句', '第二句'],
        },
    )]
    assert receipt.external_message_ids == []


async def test_qq_driver_pushes_group_payload() -> None:
    """群聊回复沿用同一条 qq.send 通道，并保留群号作为外部目标。"""
    calls: list[tuple[int, str, Any]] = []

    async def push(stream_id: int, channel: str, payload: Any) -> int:
        calls.append((stream_id, channel, payload))
        return 1

    driver = QqWebSocketDriver(push)
    receipt = await driver.send(OutboundMessage(
        stream=_qq_stream(kind='group', external_id='86420'),
        segments=['群聊回复'],
    ))

    assert calls == [(
        2,
        'qq.send',
        {
            'streamKind': 'group',
            'streamExternalId': '86420',
            'segments': ['群聊回复'],
        },
    )]
    assert receipt.external_message_ids == []


async def test_qq_driver_rejects_zero_subscribers() -> None:
    async def push(_stream_id: int, _channel: str, _payload: Any) -> int:
        return 0

    with pytest.raises(DeliveryError, match='没有适配器 WebSocket 订阅者'):
        await QqWebSocketDriver(push).send(OutboundMessage(
            stream=_qq_stream(),
            segments=['不能静默丢失'],
        ))


async def test_qq_chat_skips_parse_events_and_keeps_tagged_history(db) -> None:
    pushed: list[tuple[int, str, Any]] = []

    async def push(channel: str, payload: Any, stream_id: int = 1) -> int:
        pushed.append((stream_id, channel, payload))
        return 1

    async def driver_push(stream_id: int, channel: str, payload: Any) -> int:
        return await push(channel, payload, stream_id)

    broker = PlatformBroker()
    stream = _qq_stream()
    broker.register(stream.id, QqWebSocketDriver(driver_push))
    chat = ChatService(
        db=db,
        chat_provider=_Provider('<say emotion="smile">第一句</say><say>第二句</say>'),
        proactive_provider=None,
        summary_provider=None,
        push_event=push,
        broker=broker,
        cfg=Config(),
    )
    context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id=stream.external_id,
        sender_external_id='owner-qq',
        sender_nickname='主人',
        sender_group_card='',
        first_seen_at=1_700_000_000_000,
    )

    await chat.send(InboundMessage(text='记得我喜欢咖啡', context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert [channel for _, channel, _ in pushed if channel == 'chat.event'] == []
    qq_payloads = [payload for _, channel, payload in pushed if channel == 'qq.send']
    assert len(qq_payloads) == 1
    assert qq_payloads[0]['segments'] == ['第一句', '第二句']
    history = chat.memory.working_memory(context.stream.id)
    assert history[-1].content == '<say emotion="smile">第一句</say><say>第二句</say>'
