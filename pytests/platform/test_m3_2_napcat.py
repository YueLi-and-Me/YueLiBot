"""验证 QQ 适配器的传输、重连、运行器和配置加载行为。

本模块覆盖主体不可达、单条消息失败、提交超时、账号校验和启用字段校验，
依赖 Napcat 适配器的实际 HTTP/WebSocket 边界。
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import asyncio
import json

import httpx
import pytest
from websockets.asyncio.server import Server, serve

from src.platforms.onebot11.backend import _parse_outbound
from src.platforms.onebot11.config import (
    GroupAccessConfig,
    ProtocolConnectionConfig,
    AdapterDocument,
    PrivateAccessConfig,
    load_config,
    read_config,
)
from src.platforms.onebot11.runner import (
    OneBot11Runner,
    _parse_group_history,
)
from src.platforms.onebot11.transport import (
    OneBot11Transport,
    ProtocolAuthenticationError,
    TransportDisconnected,
)


def _transport_config(
    port: int,
    timeout: float = 0.2,
    *,
    enabled: bool = True,
    self_qq: str = '13579',
) -> ProtocolConnectionConfig:
    return ProtocolConnectionConfig(
        enabled=enabled,
        self_qq=self_qq,
        host='127.0.0.1',
        port=port,
        token='protocol-secret',
        reconnect_interval_sec=0.01,
        action_timeout_sec=timeout,
    )


def _document(
    port: int,
    *,
    enabled: bool = True,
    group: GroupAccessConfig | None = None,
    private: PrivateAccessConfig | None = None,
    self_qq: str = '13579',
) -> AdapterDocument:
    return AdapterDocument(
        inner={'version': '0.1.0'},
        napcat=_transport_config(port, enabled=enabled, self_qq=self_qq),
        owner={'qq': '24680'},
        private=private or PrivateAccessConfig(),
        group=group or GroupAccessConfig(),
    )


async def _start_server(handler: Any) -> tuple[Server, int]:
    server = await serve(handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _stop_server(server: Server) -> None:
    server.close()
    await server.wait_closed()


async def test_transport_matches_concurrent_actions_by_echo() -> None:
    async def handler(websocket: Any) -> None:
        async for raw in websocket:
            request = json.loads(raw)
            if request['action'] == 'get_login_info':
                assert websocket.request.headers['Authorization'] == 'Bearer protocol-secret'
                await websocket.send(json.dumps({
                    'status': 'ok',
                    'retcode': 0,
                    'data': {'user_id': 13579, 'nickname': '月璃'},
                    'echo': request['echo'],
                }))
                continue
            requests.append(request)
            if len(requests) == 3:
                for item in reversed(requests):
                    await websocket.send(json.dumps({
                        'status': 'ok',
                        'retcode': 0,
                        'data': {'index': item['params']['index']},
                        'echo': item['echo'],
                    }))

    requests: list[dict[str, Any]] = []
    server, port = await _start_server(handler)
    transport = OneBot11Transport(_transport_config(port))
    try:
        assert await transport.connect() == '13579'
        assert transport.self_name == '月璃'
        responses = await asyncio.gather(*[
            transport.call_action('test_action', {'index': index})
            for index in range(3)
        ])
        assert [response['data']['index'] for response in responses] == [0, 1, 2]
        assert transport.pending_count == 0
    finally:
        await transport.close()
        await _stop_server(server)


async def test_transport_timeout_cleans_pending_future() -> None:
    async def handler(websocket: Any) -> None:
        async for raw in websocket:
            request = json.loads(raw)
            if request['action'] == 'get_login_info':
                await websocket.send(json.dumps({
                    'status': 'ok', 'retcode': 0,
                    'data': {'user_id': 13579, 'nickname': '月璃'}, 'echo': request['echo'],
                }))

    server, port = await _start_server(handler)
    transport = OneBot11Transport(_transport_config(port, timeout=0.03))
    try:
        await transport.connect()
        with pytest.raises(asyncio.TimeoutError):
            await transport.call_action('never_replies')
        assert transport.pending_count == 0
    finally:
        await transport.close()
        await _stop_server(server)


async def test_transport_disconnect_fails_all_pending_actions() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(websocket: Any) -> None:
        async for raw in websocket:
            request = json.loads(raw)
            if request['action'] == 'get_login_info':
                await websocket.send(json.dumps({
                    'status': 'ok', 'retcode': 0,
                    'data': {'user_id': 13579, 'nickname': '月璃'}, 'echo': request['echo'],
                }))
                continue
            requests.append(request)
            if len(requests) == 3:
                await websocket.close()
                return

    server, port = await _start_server(handler)
    transport = OneBot11Transport(_transport_config(port, timeout=1.0))
    try:
        await transport.connect()
        results = await asyncio.gather(*[
            transport.call_action('disconnects', {'index': index})
            for index in range(3)
        ], return_exceptions=True)
        assert all(isinstance(result, TransportDisconnected) for result in results)
        assert transport.pending_count == 0
    finally:
        await transport.close()
        await _stop_server(server)


async def test_transport_event_queue_preserves_arrival_order() -> None:
    async def handler(websocket: Any) -> None:
        async for raw in websocket:
            request = json.loads(raw)
            if request['action'] == 'get_login_info':
                await websocket.send(json.dumps({
                    'status': 'ok', 'retcode': 0,
                    'data': {'user_id': 13579, 'nickname': '月璃'}, 'echo': request['echo'],
                }))
                for index in range(20):
                    await websocket.send(json.dumps({
                        'post_type': 'meta_event',
                        'meta_event_type': 'heartbeat',
                        'index': index,
                    }))
                return

    server, port = await _start_server(handler)
    transport = OneBot11Transport(_transport_config(port))
    try:
        await transport.connect()
        events = [await transport.next_event() for _ in range(20)]
        assert [event['index'] for event in events] == list(range(20))
    finally:
        await transport.close()
        await _stop_server(server)


def test_backend_outbound_requires_target_and_string_segments() -> None:
    outbound = _parse_outbound({
        'stream_id': 2,
        'channel': 'qq.send',
        'payload': {
            'streamKind': 'direct',
            'streamExternalId': '24680',
            'segments': ['第一句', '第二句'],
        },
    })
    assert outbound.stream_external_id == '24680'
    assert outbound.segments == ['第一句', '第二句']


def test_napcat_config_requires_every_runtime_field(tmp_path) -> None:
    path = tmp_path / 'napcat.toml'
    path.write_text(
        '[inner]\nversion = "0.1.0"\n\n'
        '[napcat]\n'
        'enabled = true\n'
        'self_qq = "13579"\n'
        'host = "127.0.0.1"\n'
        'port = 8095\n'
        'token = ""\n'
        'reconnect_interval_sec = 5\n'
        'action_timeout_sec = 15\n\n'
        '[owner]\nqq = "24680"\n',
        encoding='utf-8',
    )
    config = read_config(path)
    assert config.napcat.port == 8095
    assert config.napcat.token == ''
    assert config.owner.qq == '24680'

    invalid_private = tmp_path / 'invalid-private.toml'
    invalid_private.write_text(
        path.read_text(encoding='utf-8')
        + '\n[private]\nmode = "allow_all"\nlist = []\n',
        encoding='utf-8',
    )
    with pytest.raises(Exception, match='whitelist|blacklist'):
        read_config(invalid_private)

    missing = path.read_text(encoding='utf-8').replace('token = ""\n', '')
    path.write_text(missing, encoding='utf-8')
    with pytest.raises(Exception):
        read_config(path)

    with pytest.raises(SystemExit):
        load_config(tmp_path / 'not-found.toml')


def test_disabled_napcat_template_accepts_empty_owner(tmp_path) -> None:
    path = tmp_path / 'napcat.toml'
    path.write_text(
        '[inner]\nversion = "0.1.0"\n\n'
        '[napcat]\n'
        'enabled = false\n'
        'host = "127.0.0.1"\n'
        'port = 8095\n'
        'token = ""\n'
        'reconnect_interval_sec = 5\n'
        'action_timeout_sec = 15\n\n'
        '[owner]\nqq = ""\n',
        encoding='utf-8',
    )

    config = read_config(path)
    assert config.napcat.enabled is False
    assert config.owner.qq == ''


@pytest.mark.asyncio
async def test_disabled_runner_does_not_connect() -> None:
    runner = OneBot11Runner(_document(1, enabled=False), backend_port=1, token='backend-secret')
    await runner.run()


class _OneEventTransport:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def iter_events(self):
        yield self._payload


class _RecordingBackend:
    def __init__(self) -> None:
        self.submitted: list[object] = []

    async def submit_inbound(self, event: object) -> None:
        self.submitted.append(event)


async def test_private_access_denial_happens_before_backend_submission() -> None:
    payload = {
        'post_type': 'message',
        'message_type': 'private',
        'message_id': 101,
        'user_id': 11111,
        'self_id': 13579,
        'message': [{'type': 'text', 'data': {'text': '不在名单'}}],
        'sender': {'user_id': 11111, 'nickname': '名单外'},
    }
    backend = _RecordingBackend()
    runner = OneBot11Runner(
        _document(
            1,
            private=PrivateAccessConfig(mode='whitelist', list=['22222']),
        ),
        backend_port=1,
        token='backend-secret',
        transport=_OneEventTransport(payload),
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert backend.submitted == []


class _RejectedTransport:
    def __init__(self) -> None:
        self.attempts = 0

    async def connect(self) -> str:
        self.attempts += 1
        raise ProtocolAuthenticationError(401)

    async def close(self) -> None:
        return None


async def test_runner_authentication_failure_exits_without_retry() -> None:
    transport = _RejectedTransport()
    runner = OneBot11Runner(
        _document(1),
        backend_port=1,
        token='backend-secret',
        transport=transport,
    )

    with pytest.raises(RuntimeError, match='停止重试'):
        await runner.run()
    assert transport.attempts == 1


class _UnavailableTransport:
    def __init__(self) -> None:
        self.attempts = 0

    async def connect(self) -> str:
        self.attempts += 1
        raise ConnectionRefusedError('协议端尚未启动')

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_runner_retries_when_protocol_endpoint_is_unavailable() -> None:
    transport = _UnavailableTransport()
    runner = OneBot11Runner(
        _document(1),
        backend_port=1,
        token='backend-secret',
        transport=transport,
    )
    task = asyncio.create_task(runner.run())
    try:
        await asyncio.sleep(0.08)
        assert not task.done(), '协议端未启动时适配器不应退出'
        assert transport.attempts >= 2
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class _BackendUnreachable:
    """主体后端不可达；httpx 的异常不继承任何内置 socket 异常，这正是要验的点。"""

    def __init__(self) -> None:
        self.attempts = 0

    async def connect(self) -> None:
        self.attempts += 1
        raise httpx.ConnectError('主体后端尚未就绪')

    async def close(self) -> None:
        return None


class _ConnectingTransport:
    self_name = '月璃'

    async def connect(self) -> str:
        return '13579'

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_runner_retries_when_backend_is_unreachable() -> None:
    """主体短暂不可达时重试，不得误判为配置错误并退出。"""
    backend = _BackendUnreachable()
    runner = OneBot11Runner(
        _document(1),
        backend_port=1,
        token='backend-secret',
        transport=_ConnectingTransport(),
        backend=backend,
    )
    task = asyncio.create_task(runner.run())
    try:
        await asyncio.sleep(0.08)
        assert not task.done(), '主体不可达时适配器不应退出'
        assert backend.attempts >= 2
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class _TwoEventTransport:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads

    async def iter_events(self):
        for payload in self._payloads:
            yield payload


class _RejectingThenAcceptingBackend:
    """第一条被主体拒绝，第二条正常收下。"""

    def __init__(self) -> None:
        self.submitted: list[object] = []

    async def submit_inbound(self, event: object) -> None:
        if not self.submitted:
            self.submitted.append(event)
            raise httpx.HTTPStatusError(
                '500 Internal Server Error',
                request=httpx.Request('POST', 'http://127.0.0.1/platform/inbound'),
                response=httpx.Response(500),
            )
        self.submitted.append(event)


class _TimingOutThenAcceptingBackend:
    """第一条提交超时，第二条正常收下。"""

    def __init__(self) -> None:
        self.submitted: list[object] = []

    async def submit_inbound(self, event: object) -> None:
        self.submitted.append(event)
        if len(self.submitted) == 1:
            raise httpx.ReadTimeout(
                '主体处理入站消息超时',
                request=httpx.Request('POST', 'http://127.0.0.1/platform/inbound'),
            )


def _private_payload(message_id: int, text: str) -> dict[str, Any]:
    return {
        'post_type': 'message',
        'message_type': 'private',
        'message_id': message_id,
        'user_id': 24680,
        'self_id': 13579,
        'message': [{'type': 'text', 'data': {'text': text}}],
        'sender': {'user_id': 24680, 'nickname': '用户本人'},
    }


@pytest.mark.asyncio
async def test_rejected_inbound_message_does_not_tear_down_the_connection() -> None:
    """主体拒收单条消息时只丢弃该条，后续消息继续提交。"""
    backend = _RejectingThenAcceptingBackend()
    runner = OneBot11Runner(
        _document(1),
        backend_port=1,
        token='backend-secret',
        transport=_TwoEventTransport([
            _private_payload(201, '第一条'),
            _private_payload(202, '第二条'),
        ]),
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert len(backend.submitted) == 2
    assert backend.submitted[1].external_message_id == '202'


@pytest.mark.asyncio
async def test_timed_out_inbound_message_does_not_tear_down_the_connection(
    capsys: Any,
) -> None:
    """单条入站提交超时只影响该条，后续消息继续消费。"""
    backend = _TimingOutThenAcceptingBackend()
    runner = OneBot11Runner(
        _document(1),
        backend_port=1,
        token='backend-secret',
        transport=_TwoEventTransport([
            _private_payload(301, '第一条超时'),
            _private_payload(302, '第二条继续'),
        ]),
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    output = capsys.readouterr().out
    assert len(backend.submitted) == 2
    assert backend.submitted[1].external_message_id == '302'
    assert 'QQ 入站消息提交超时' in output


def test_missing_enabled_field_fails_at_load_time(tmp_path) -> None:
    """enabled 为必填字段，缺失时必须在加载期报错而不是隐式启用连接。"""
    path = tmp_path / 'napcat.toml'
    path.write_text(
        '[inner]\nversion = "0.1.0"\n\n'
        '[napcat]\n'
        'host = "127.0.0.1"\n'
        'port = 8095\n'
        'token = ""\n'
        'reconnect_interval_sec = 5\n'
        'action_timeout_sec = 15\n\n'
        '[owner]\nqq = "12345"\n',
        encoding='utf-8',
    )

    with pytest.raises(Exception, match='enabled'):
        read_config(path)


def _write_config(path, *, self_qq: str, owner_qq: str) -> None:
    path.write_text(
        '[inner]\nversion = "0.1.0"\n\n'
        '[napcat]\n'
        'enabled = true\n'
        f'self_qq = "{self_qq}"\n'
        'host = "127.0.0.1"\n'
        'port = 8095\n'
        'token = ""\n'
        'reconnect_interval_sec = 5\n'
        'action_timeout_sec = 15\n\n'
        f'[owner]\nqq = "{owner_qq}"\n',
        encoding='utf-8',
    )


def test_self_qq_and_owner_qq_must_differ(tmp_path) -> None:
    """self_qq 与 owner_qq 相同时必须在加载期拒绝。"""
    path = tmp_path / 'napcat.toml'
    _write_config(path, self_qq='3622760052', owner_qq='3622760052')

    with pytest.raises(Exception, match='必须是不同的号'):
        read_config(path)


def test_self_qq_required_when_enabled(tmp_path) -> None:
    """适配器启用但未填写 self_qq 时必须报错。"""
    path = tmp_path / 'napcat.toml'
    _write_config(path, self_qq='', owner_qq='900000001')

    with pytest.raises(Exception, match='self_qq'):
        read_config(path)


def test_two_distinct_qq_numbers_load_fine(tmp_path) -> None:
    path = tmp_path / 'napcat.toml'
    _write_config(path, self_qq='3622760052', owner_qq='900000001')

    config = read_config(path)
    assert config.napcat.self_qq == '3622760052'
    assert config.owner.qq == '900000001'


class _LoggedInAsTransport:
    """协议端登录的是另一个号，用来验配置与现场对不上的情形。"""

    def __init__(self, self_id: str) -> None:
        self.self_id = self_id
        self.self_name = '月璃'
        self.attempts = 0

    async def connect(self) -> str:
        self.attempts += 1
        return self.self_id

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_runner_exits_when_self_qq_does_not_match_protocol_login() -> None:
    """配置中的 self_qq 与协议端登录账号不一致时立即退出并报告两者差异。"""
    transport = _LoggedInAsTransport('3622760052')
    runner = OneBot11Runner(
        _document(1, self_qq='9999999999'),
        backend_port=1,
        token='backend-secret',
        transport=transport,
    )

    with pytest.raises(RuntimeError, match='3622760052'):
        await runner.run()
    assert transport.attempts == 1

class _ImageSourceBackend:
    def __init__(self) -> None:
        self.submitted: list[object] = []

    async def submit_inbound(self, event: object) -> None:
        self.submitted.append(event)


class _ResolvingImageTransport(_TwoEventTransport):
    """gchat 图片来源返回 NapCat 本地绝对路径。"""

    def __init__(self, payload: dict[str, Any], local_path: str) -> None:
        super().__init__([payload])
        self._local_path = local_path
        self.actions: list[tuple[str, dict[str, Any]]] = []

    async def call_action(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.actions.append((action, dict(params or {})))
        return {
            'status': 'ok',
            'retcode': 0,
            'data': {'file': self._local_path},
        }


@pytest.mark.asyncio
async def test_runner_resolves_qpic_image_source_to_local_file(tmp_path: Path) -> None:
    """gchat.qpic.cn 来源先经 NapCat 解析为本地路径，绕开 CDN 防盗链。"""
    local_path = tmp_path / 'image.jpg'
    payload = {
        'post_type': 'message',
        'message_type': 'group',
        'message_id': 902,
        'group_id': 86420,
        'user_id': 97531,
        'self_id': 13579,
        'message': [
            {'type': 'image', 'data': {
                'sub_type': 0,
                'file': 'image.jpg',
                'url': 'https://gchat.qpic.cn/download?appid=1406&fileid=test',
            }},
        ],
        'sender': {'user_id': 97531, 'nickname': '群友'},
    }
    transport = _ResolvingImageTransport(payload, str(local_path))
    backend = _ImageSourceBackend()
    runner = OneBot11Runner(
        _document(1, group=GroupAccessConfig(mode='whitelist', list=['86420'])),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert len(backend.submitted) == 1
    event = backend.submitted[0]
    assert event.image_sources == (local_path.as_uri(),)
    assert ('get_image', {'file': 'image.jpg'}) in transport.actions


@pytest.mark.asyncio
async def test_runner_submits_image_source_without_downloading() -> None:
    """串行入站循环只提交来源引用，不在适配器侧读取图片字节。"""
    payload = {
        'post_type': 'message',
        'message_type': 'group',
        'message_id': 902,
        'group_id': 86420,
        'user_id': 97531,
        'self_id': 13579,
        'message': [
            {'type': 'image', 'data': {'sub_type': 0, 'url': 'file://missing-image.png'}},
        ],
        'sender': {'user_id': 97531, 'nickname': '群友'},
    }
    backend = _ImageSourceBackend()
    runner = OneBot11Runner(
        _document(1, group=GroupAccessConfig(mode='whitelist', list=['86420'])),
        backend_port=1,
        token='backend-secret',
        transport=_TwoEventTransport([payload]),
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert len(backend.submitted) == 1
    event = backend.submitted[0]
    assert event.image_sources == ('file://missing-image.png',)


@pytest.mark.asyncio
async def test_inbound_event_keeps_image_source_without_downloading() -> None:
    """入站事件只携带图片来源引用，下载与描述交给主体后台任务。"""
    from src.platforms.onebot11.events import parse_inbound_event

    event = parse_inbound_event(
        {
            'post_type': 'message',
            'message_type': 'group',
            'message_id': 901,
            'group_id': 86420,
            'user_id': 97531,
            'self_id': 13579,
            'message': [
                {'type': 'text', 'data': {'text': '看'}},
                {'type': 'image', 'data': {'sub_type': 0, 'url': 'base64://AQID'}},
            ],
            'sender': {'user_id': 97531, 'nickname': '群友'},
        },
        '13579',
        '月璃',
        '24680',
        PrivateAccessConfig(),
        GroupAccessConfig(list=['86420']),
    )
    assert event is not None
    assert event.image_sources == ('base64://AQID',)


def test_group_history_response_is_normalized_to_backfill_messages() -> None:
    response = {
        'data': {
            'messages': [
                {
                    'message_id': 1001,
                    'message_seq': 10,
                    'time': 1_750_000_000,
                    'user_id': 97531,
                    'sender': {'user_id': 97531, 'nickname': '群友', 'card': '小李'},
                    'message': [{'type': 'text', 'data': {'text': '停机期间的话'}}],
                },
            ],
        },
    }

    messages = _parse_group_history(
        response,
        '86420',
        '13579',
        '月璃',
        '24680',
        PrivateAccessConfig(),
        GroupAccessConfig(list=['86420']),
    )

    assert len(messages) == 1
    assert messages[0]['externalMessageId'] == '1001'
    assert messages[0]['messageSeq'] == 10
    assert messages[0]['createdAt'] == 1_750_000_000_000
    assert messages[0]['text'] == '停机期间的话'
    assert messages[0]['senderExternalId'] == '97531'


class _BackfillTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.actions: list[tuple[str, dict[str, Any]]] = []

    async def call_action(
        self,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.actions.append((action, dict(params or {})))
        return self.response


class _BackfillBackend:
    def __init__(self) -> None:
        self.groups: list[tuple[str, list[dict[str, Any]]]] = []

    async def submit_group_backfill(
        self,
        group_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        self.groups.append((group_id, messages))


@pytest.mark.asyncio
async def test_runner_backfills_whitelist_group_history_without_triggering_reply() -> None:
    response = {
        'data': {
            'messages': [
                {
                    'message_id': 2001,
                    'message_seq': 5,
                    'time': 1_750_000_000,
                    'user_id': 97531,
                    'sender': {'user_id': 97531, 'nickname': '群友'},
                    'message': [{'type': 'text', 'data': {'text': '补一条历史'}}],
                },
            ],
        },
    }
    backend = _BackfillBackend()
    transport = _BackfillTransport(response)
    runner = OneBot11Runner(
        _document(1, group=GroupAccessConfig(mode='whitelist', list=['86420'])),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._backfill_recent_group_history('13579', '月璃')

    assert transport.actions == [
        ('get_group_msg_history', {'group_id': 86420, 'count': 20, 'reverse_order': True}),
    ]
    assert len(backend.groups) == 1
    assert backend.groups[0][0] == '86420'
    assert backend.groups[0][1][0]['externalMessageId'] == '2001'
