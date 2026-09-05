"""QQ 群名称跨进程回传、刷新与失败降级回归。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import json

import httpx

from src.core.api.http import (
    PlatformGroupDisplayNameBody,
    platform_group_display_name,
    streams,
)
from src.core.api.state import app_state
from src.core.platform_io.registry import StreamRegistry
from src.platforms.onebot11.backend import BackendClient
from src.platforms.onebot11.config import (
    AdapterDocument,
    GroupAccessConfig,
    PrivateAccessConfig,
    ProtocolConnectionConfig,
)
from src.platforms.onebot11.runner import OneBot11Runner
import src.platforms.onebot11.runner as runner_module


def _document(groups: List[str]) -> AdapterDocument:
    """构造启用状态的最小 QQ 适配器配置。"""

    return AdapterDocument(
        inner={'version': '0.1.0'},
        napcat=ProtocolConnectionConfig(
            enabled=True,
            self_qq='13579',
            host='127.0.0.1',
            port=8095,
            token='',
            reconnect_interval_sec=5,
            action_timeout_sec=15,
        ),
        owner={'qq': '24680'},
        private=PrivateAccessConfig(),
        group=GroupAccessConfig(mode='whitelist', list=groups),
    )


def _group_payload(message_id: int, group_id: int) -> Dict[str, Any]:
    """构造一条无需额外解析 action 的白名单群文字消息。"""

    return {
        'post_type': 'message',
        'message_type': 'group',
        'message_id': message_id,
        'group_id': group_id,
        'user_id': 97531,
        'self_id': 13579,
        'message': [{'type': 'text', 'data': {'text': f'第 {message_id} 条'}}],
        'sender': {'user_id': 97531, 'nickname': '群友'},
    }


class _GroupNameTransport:
    """同时提供有限事件流与可编排的 get_group_info 响应。"""

    def __init__(
        self,
        payloads: List[Dict[str, Any]],
        *,
        group_name: str = '月璃的小窝',
        error: Exception | None = None,
    ) -> None:
        self._payloads = payloads
        self._group_name = group_name
        self._error = error
        self.actions: List[tuple[str, Dict[str, Any]]] = []

    async def iter_events(self) -> AsyncIterator[Dict[str, Any]]:
        for payload in self._payloads:
            yield payload

    async def call_action(
        self,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.actions.append((action, params))
        if self._error is not None:
            raise self._error
        return {
            'status': 'ok',
            'retcode': 0,
            'data': {'group_id': params['group_id'], 'group_name': self._group_name},
        }


class _GroupNameBackend:
    """记录入站消息与群名称回传，不建立真实网络连接。"""

    def __init__(self) -> None:
        self.submitted: List[object] = []
        self.names: List[tuple[str, str]] = []

    async def submit_inbound(self, event: object) -> None:
        self.submitted.append(event)

    async def report_group_display_name(self, group_id: str, display_name: str) -> None:
        self.names.append((group_id, display_name))


class _LogRecorder:
    """只记录本用例预期触发的可见警告。"""

    def __init__(self) -> None:
        self.warnings: List[tuple[str, Dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.warnings.append((event, fields))


async def test_backend_posts_group_display_name_to_dedicated_endpoint() -> None:
    """适配器复用既有鉴权 HTTP 方向，不把群名伪装成入站消息。"""

    requests: List[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={'ok': True, 'streamId': 7})

    client = BackendClient(1, 'backend-secret')
    client._http = httpx.AsyncClient(
        base_url='http://127.0.0.1:1',
        transport=httpx.MockTransport(handle),
    )
    try:
        await client.report_group_display_name('629201002', '月璃的小窝')
    finally:
        await client.close()

    assert len(requests) == 1
    assert requests[0].url.path == '/platform/group/display-name'
    assert json.loads(requests[0].content) == {
        'platform': 'qq',
        'streamExternalId': '629201002',
        'displayName': '月璃的小窝',
    }


async def test_group_display_name_endpoint_creates_and_updates_stream(db) -> None:
    """启动刷新可先于首条消息建 stream，随后刷新只更新可读名称。"""

    previous_registry = app_state.registry
    previous_register = app_state.register_platform_stream
    registered = []
    app_state.registry = StreamRegistry(db)
    app_state.register_platform_stream = registered.append
    try:
        first_response = await platform_group_display_name(PlatformGroupDisplayNameBody(
            platform='qq',
            streamExternalId='629201002',
            displayName='  月璃的小窝  ',
        ))
        second_response = await platform_group_display_name(PlatformGroupDisplayNameBody(
            platform='qq',
            streamExternalId='629201002',
            displayName='月璃的新窝',
        ))
        stream_listing = await streams()
    finally:
        app_state.registry = previous_registry
        app_state.register_platform_stream = previous_register

    first_payload = json.loads(first_response.body)
    second_payload = json.loads(second_response.body)
    assert first_payload['ok'] is True
    assert second_payload['streamId'] == first_payload['streamId']
    stream = StreamRegistry(db).stream(second_payload['streamId'])
    assert stream.kind == 'group'
    assert stream.external_id == '629201002'
    assert stream.display_name == '月璃的新窝'
    listed = next(item for item in stream_listing['streams'] if item['id'] == stream.id)
    assert listed['displayName'] == '月璃的新窝'
    assert [item.display_name for item in registered] == ['月璃的小窝', '月璃的新窝']


async def test_process_start_refreshes_each_whitelisted_group_once() -> None:
    """同一适配器进程的启动刷新只调一次，两个群可并发且互不覆盖。"""

    transport = _GroupNameTransport([])
    backend = _GroupNameBackend()
    runner = OneBot11Runner(
        _document(['629201002', '629201003']),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    runner._schedule_startup_group_name_refresh()
    runner._schedule_startup_group_name_refresh()
    await runner._finish_group_name_refreshes()

    assert transport.actions == [
        ('get_group_info', {'group_id': 629201002}),
        ('get_group_info', {'group_id': 629201003}),
    ]
    assert backend.names == [
        ('629201002', '月璃的小窝'),
        ('629201003', '月璃的小窝'),
    ]


async def test_group_name_failure_does_not_block_messages_and_logs_once(monkeypatch) -> None:
    """D-6：同群首次拉取失败只留一条警告，两条消息仍照常提交。"""

    transport = _GroupNameTransport(
        [_group_payload(1, 629201002), _group_payload(2, 629201002)],
        error=RuntimeError('协议端不支持 get_group_info'),
    )
    backend = _GroupNameBackend()
    logs = _LogRecorder()
    monkeypatch.setattr(runner_module, 'logger', logs)
    runner = OneBot11Runner(
        _document(['629201002']),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')
    await runner._finish_group_name_refreshes()

    assert len(backend.submitted) == 2
    assert backend.names == []
    assert transport.actions == [('get_group_info', {'group_id': 629201002})]
    assert logs.warnings == [(
        'QQ 群名称拉取失败，继续处理消息',
        {
            'groupId': '629201002',
            'error': '协议端不支持 get_group_info',
        },
    )]
