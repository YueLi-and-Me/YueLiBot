"""验证 owner QQ 身份绑定与适配器启动顺序。

本模块覆盖身份写入、运行时连接信息读取和适配器启动前的归属检查，
确保平台用户身份不会在数据库和运行时连接之间发生错配。
"""

from __future__ import annotations

from typing import Any, cast

import asyncio
import sqlite3

import pytest

from src.platforms.onebot11.config import ProtocolConnectionConfig, AdapterDocument
from src.platforms.onebot11.runner import OneBot11Runner
from src.platforms.onebot11.transport import OneBot11Transport
from src.core.api.http import PlatformIdentityLinkBody, platform_identity_link
from src.core.api.state import app_state
from src.core.platform_io.registry import StreamRegistry


FIRST_SEEN_AT = 1_700_000_000_000


def _document() -> AdapterDocument:
    return AdapterDocument(
        inner={'version': '0.1.0'},
        napcat=ProtocolConnectionConfig(
            enabled=True,
            self_qq='13579',
            host='127.0.0.1',
            port=8095,
            token='',
            reconnect_interval_sec=0.01,
            action_timeout_sec=0.2,
        ),
        owner={'qq': '24680'},
    )


def test_owner_binding_keeps_qq_in_the_seeded_owner(db: sqlite3.Connection) -> None:
    registry = StreamRegistry(db)
    owner = registry.owner_person()

    registry.link_identity(owner, 'qq', '24680', '24680')
    context = registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='24680',
        sender_external_id='24680',
        sender_nickname='主人',
        sender_group_card='',
        first_seen_at=FIRST_SEEN_AT,
    )

    assert context.person == owner
    assert db.execute('SELECT COUNT(*) FROM persons').fetchone()[0] == 1
    assert registry.display_name(owner.id, 'qq') == '主人'


async def test_identity_route_links_the_existing_owner(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = StreamRegistry(db)
    monkeypatch.setattr(app_state, 'registry', registry)

    body = PlatformIdentityLinkBody.model_validate({
        'platform': 'qq',
        'externalId': '24680',
        'displayName': '24680',
    })
    result = await platform_identity_link(body)

    assert result == {'ok': True, 'personId': 1}
    assert registry.find_person_by_identity('qq', '24680') == registry.owner_person()


class _TransportProbe:
    self_name = '月璃'

    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def connect(self) -> str:
        self._order.append('transport.connect')
        return '13579'

    async def close(self) -> None:
        self._order.append('transport.close')


class _BackendProbe:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def connect(self) -> None:
        self._order.append('backend.connect')

    async def link_owner_identity(self, owner_qq: str) -> None:
        self._order.append(f'backend.link:{owner_qq}')

    async def close(self) -> None:
        self._order.append('backend.close')


class _StopAfterLinkRunner(OneBot11Runner):
    async def _serve_connected(self, self_id: str, self_name: str) -> None:
        del self_id, self_name
        raise asyncio.CancelledError


async def test_runner_binds_owner_before_consuming_messages() -> None:
    order: list[str] = []
    runner = _StopAfterLinkRunner(
        _document(),
        backend_port=57081,
        token='backend-secret',
        transport=cast(OneBot11Transport, _TransportProbe(order)),
        backend=cast(Any, _BackendProbe(order)),
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run()

    # 连接循环以 finally 兜底关闭：CancelledError 透传后仍先关主体再关协议端，
    # 否则中断时会泄漏连接。断言同时覆盖建链顺序与这段收尾顺序。
    assert order == [
        'transport.connect',
        'backend.connect',
        'backend.link:24680',
        'backend.close',
        'transport.close',
    ]
