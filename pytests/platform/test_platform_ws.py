"""验证 WebSocket 观察端点对多个订阅者的事件隔离。

本模块确认连接管理器可以独立推送、移除和清理订阅者，
并保持事件账本与实时广播的一致性。
"""

from __future__ import annotations

import json
from typing import List

from src.core.api.ws import _ConnectionManager
from src.core.observe.events import reset_for_tests
from src.core.observe.store import event_store


class _WebSocket:
    def __init__(self) -> None:
        self.messages: List[str] = []

    async def send_text(self, message: str) -> None:
        self.messages.append(message)


async def test_push_isolated_by_client_and_carries_stream_id() -> None:
    manager = _ConnectionManager()
    desktop = _WebSocket()
    napcat = _WebSocket()
    await manager.connect('desktop', desktop)  # type: ignore[arg-type]
    await manager.connect('platform', napcat)  # type: ignore[arg-type]

    assert await manager.push(1, 'voice.play', {'audio': 'base64'}) == 1
    assert await manager.push(2, 'chat.event', {'text': '你好'}) == 1

    assert [json.loads(message) for message in desktop.messages] == [{
        'stream_id': 1,
        'channel': 'voice.play',
        'payload': {'audio': 'base64'},
    }]
    assert [json.loads(message) for message in napcat.messages] == [{
        'stream_id': 2,
        'channel': 'chat.event',
        'payload': {'text': '你好'},
    }]


async def test_napcat_delivery_loss_is_traced_but_desktop_remains_quiet() -> None:
    manager = _ConnectionManager()
    event_store.clear()
    reset_for_tests()

    assert await manager.push(2, 'chat.event', {}) == 0
    assert event_store.since(0).events[-1]['kind'] == 'outbound_dropped'
    event_store.clear()

    assert await manager.push(1, 'chat.event', {}) == 0
    assert event_store.since(0).events == []
