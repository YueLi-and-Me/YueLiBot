"""验证合并转发从 QQ 事件到主体 HTTP 载荷的完整入站链。"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import json

import httpx
import pytest

from src.core.api.http import PlatformInboundBody
from src.platforms.onebot11.backend import BackendClient
from src.platforms.onebot11.config import (
    GroupAccessConfig,
    ProtocolConnectionConfig,
    AdapterDocument,
    OwnerConfig,
    PrivateAccessConfig,
)
from src.platforms.onebot11.events import QqInboundEvent
from src.platforms.onebot11.forward import unreadable_forward_tree
from src.platforms.onebot11.runner import (
    OneBot11Runner,
    _mark_forward_unreadable,
    _replace_forward_placeholders,
)
from src.platforms.onebot11.transport import ActionError


def _document() -> AdapterDocument:
    return AdapterDocument(
        inner={'version': '0.1.0'},
        napcat=ProtocolConnectionConfig(
            enabled=True,
            self_qq='13579',
            host='127.0.0.1',
            port=1,
            token='protocol-secret',
            reconnect_interval_sec=0.01,
            action_timeout_sec=0.2,
        ),
        owner=OwnerConfig(qq='24680'),
        private=PrivateAccessConfig(mode='whitelist', list=[]),
        group=GroupAccessConfig(mode='whitelist', list=['86420']),
    )


def _payload(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'post_type': 'message',
        'message_type': 'group',
        'message_id': 5001,
        'group_id': 86420,
        'user_id': 97531,
        'self_id': 13579,
        'message': [{'type': 'forward', 'data': data}],
        'sender': {'user_id': 97531, 'nickname': '群友甲', 'card': '甲'},
    }


def _forward_messages(text: str = '内层正文') -> List[Dict[str, Any]]:
    return [{
        'sender': {'nickname': '内层用户', 'card': ''},
        'message': [{'type': 'text', 'data': {'text': text}}],
    }]


class _Transport:
    def __init__(
        self,
        payload: Dict[str, Any],
        response: Dict[str, Any] | None = None,
        nested: Dict[str, Any] | None = None,
    ) -> None:
        self._payload = payload
        self._response = response
        # 按资源编号给出的响应，用于区分根转发与逐层补取的嵌套转发；
        # 值为异常实例时表示该层取内容失败。
        self._nested = nested or {}
        self.actions: List[Tuple[str, Dict[str, Any]]] = []

    async def iter_events(self):
        yield self._payload

    async def call_action(
        self,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.actions.append((action, params))
        message_id = str(params.get('message_id') or '')
        if message_id in self._nested:
            result = self._nested[message_id]
            if isinstance(result, Exception):
                raise result
            return result
        if self._response is None:
            raise AssertionError(f'未预期的 action：{action}')
        return self._response


class _Backend:
    def __init__(self) -> None:
        self.events: List[QqInboundEvent] = []

    async def submit_inbound(self, event: QqInboundEvent) -> None:
        self.events.append(event)


async def test_runner_fetches_forward_tree_before_submission() -> None:
    """事件只含资源编号时，应先取完整树再提交主体。"""
    transport = _Transport(
        _payload({'id': 'root-forward'}),
        {'data': {'messages': _forward_messages()}},
    )
    backend = _Backend()
    runner = OneBot11Runner(
        _document(),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert transport.actions == [
        ('get_forward_msg', {'message_id': 'root-forward'}),
    ]
    assert len(backend.events) == 1
    event = backend.events[0]
    assert event.text == '[转发消息：内层用户：内层正文｜共 1 条]'
    assert event.forward_messages[0].nodes[0].sender_name == '内层用户'
    assert event.forward_messages[0].nodes[0].parts[0].text == '内层正文'


async def test_runner_uses_inline_forward_content_without_network_call() -> None:
    """事件已内联完整正文时不重复请求协议端。"""
    transport = _Transport(_payload({'id': 'root', 'content': _forward_messages('内联')}))
    backend = _Backend()
    runner = OneBot11Runner(
        _document(),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert transport.actions == []
    assert backend.events[0].forward_messages[0].nodes[0].parts[0].text == '内联'


def _nested_root_response(nested_id: str) -> Dict[str, Any]:
    """构造一层只含嵌套转发编号、不内联内层正文的根响应。"""
    return {'data': {'messages': [{
        'sender': {'nickname': '外层用户'},
        'message': [{'type': 'forward', 'data': {'id': nested_id}}],
    }]}}


async def test_runner_fetches_nested_forward_content_by_id() -> None:
    """协议端不内联内层正文时，按编号逐层补取而不是丢弃整棵根树。"""
    transport = _Transport(
        _payload({'id': 'root'}),
        _nested_root_response('inner'),
        nested={'inner': {'data': {'messages': _forward_messages('内层正文')}}},
    )
    backend = _Backend()
    runner = OneBot11Runner(
        _document(),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert transport.actions == [
        ('get_forward_msg', {'message_id': 'root'}),
        ('get_forward_msg', {'message_id': 'inner'}),
    ]
    tree = backend.events[0].forward_messages[0]
    nested = tree.nodes[0].parts[0].nested
    assert nested is not None
    assert nested.nodes[0].parts[0].text == '内层正文'


async def test_runner_degrades_nested_fetch_failure_to_text_part() -> None:
    """内层取内容失败时根树保留，内层降级为文本片段占位。"""
    transport = _Transport(
        _payload({'id': 'root'}),
        _nested_root_response('inner'),
        nested={'inner': ActionError('get_forward_msg', {'status': 'failed'})},
    )
    backend = _Backend()
    runner = OneBot11Runner(
        _document(),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert len(backend.events) == 1
    event = backend.events[0]
    # 根解析成功：正文换成含预览的形态，树照常暴露给读取工具；降级的
    # 嵌套层在预览里以失败文本出现，如实反映「这一层读不到」。
    assert event.text == (
        '[转发消息：外层用户：[这一层的转发内容读取失败]｜共 1 条]'
    )
    assert len(event.forward_messages) == 1
    part = event.forward_messages[0].nodes[0].parts[0]
    assert part.kind == 'text'
    assert part.text == '[这一层的转发内容读取失败]'


async def test_runner_marks_whole_message_unreadable_when_root_fetch_fails() -> None:
    """根级取内容失败（全部根失败）时与基线一致：不暴露树、正文标记失败。"""
    transport = _Transport(
        _payload({'id': 'root'}),
        nested={'root': ActionError('get_forward_msg', {'status': 'failed'})},
    )
    backend = _Backend()
    runner = OneBot11Runner(
        _document(),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert len(backend.events) == 1
    # 读取工具的声明按会话给出，可读与不可读的转发在正文里必须长得不一样，
    # 否则模型只能挨个编号试到失败。
    assert backend.events[0].text == '[转发消息：内容读取失败]'
    assert backend.events[0].forward_messages == ()


async def test_runner_keeps_root_count_with_placeholder_tree_on_partial_failure() -> None:
    """三根转发中间失败：根数不变、失败位是占位树、正文按位置区分形态。"""
    payload = {
        'post_type': 'message',
        'message_type': 'group',
        'message_id': 5002,
        'group_id': 86420,
        'user_id': 97531,
        'self_id': 13579,
        'message': [
            {'type': 'text', 'data': {'text': '转发合集：'}},
            {'type': 'forward', 'data': {'id': 'first'}},
            {'type': 'forward', 'data': {'id': 'second'}},
            {'type': 'forward', 'data': {'id': 'third'}},
        ],
        'sender': {'user_id': 97531, 'nickname': '群友甲', 'card': '甲'},
    }
    transport = _Transport(
        payload,
        nested={
            'first': {'data': {'messages': _forward_messages('第一条')}},
            'second': ActionError('get_forward_msg', {'status': 'failed'}),
            'third': {'data': {'messages': _forward_messages('第三条')}},
        },
    )
    backend = _Backend()
    runner = OneBot11Runner(
        _document(),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert len(backend.events) == 1
    event = backend.events[0]
    # 根数量与正文占位个数一一对应是工具编号对齐的唯一依据。
    assert len(event.forward_messages) == 3
    assert event.forward_messages[0].nodes[0].parts[0].text == '第一条'
    assert event.forward_messages[1] == unreadable_forward_tree()
    assert event.forward_messages[2].nodes[0].parts[0].text == '第三条'
    # 第 i 个占位只反映第 i 个根的结果：成功根是预览，失败根是失败形态。
    assert event.text == (
        '转发合集：'
        '[转发消息：内层用户：第一条｜共 1 条]'
        '[转发消息：内容读取失败]'
        '[转发消息：内层用户：第三条｜共 1 条]'
    )


def test_forward_placeholder_replacement_aligns_by_position() -> None:
    """按位置替换的原文保持，与个数不对齐时的退路。"""
    assert _replace_forward_placeholders(
        '甲[转发消息]乙[转发消息]丙', ['A', 'B'],
    ) == '甲A乙B丙'
    # 占位个数与根数不一致（如被引用摘要混入同形文本）时不做任何替换。
    assert _replace_forward_placeholders('甲[转发消息]乙[转发消息]丙', ['A']) is None
    # 全失败分支保留全量替换语义：不存在可读的转发，多标不冤枉任何树。
    assert _mark_forward_unreadable('[转发消息] x [转发消息]', 1) == (
        '[转发消息：内容读取失败] x [转发消息：内容读取失败]'
    )


async def test_backend_serializes_forward_tree_only_when_present() -> None:
    """主体 HTTP 新字段保留完整树，普通消息载荷不凭空增加字段。"""
    seen: List[Dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(json.loads(request.content)))
        return httpx.Response(200, json={'accepted': True})

    client = BackendClient(1, 'backend-secret')
    client._http = httpx.AsyncClient(
        base_url='http://127.0.0.1:1',
        transport=httpx.MockTransport(handler),
    )
    transport = _Transport(
        _payload({'id': 'root'}),
        {'data': {'messages': _forward_messages()}},
    )
    backend = _Backend()
    runner = OneBot11Runner(
        _document(),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )
    await runner._consume_protocol_events('13579', '月璃')
    event = backend.events[0]

    try:
        await client.submit_inbound(event)
        await client.submit_inbound(QqInboundEvent(
            stream_kind='group',
            stream_external_id='86420',
            sender_external_id='97531',
            sender_nickname='群友甲',
            sender_group_card='甲',
            bot_name='月璃',
            text='普通消息',
            mentioned_me=False,
            external_message_id='5002',
        ))
    finally:
        await client.close()

    assert seen[0]['forwardMessages'][0]['nodes'][0]['senderName'] == '内层用户'
    assert 'forwardMessages' not in seen[1]


def test_platform_inbound_model_rejects_broken_forward_tree() -> None:
    """主体请求模型在产生归属和落库副作用前拒绝半合法消息树。"""
    base = {
        'platform': 'qq',
        'streamKind': 'group',
        'streamExternalId': '86420',
        'senderExternalId': '97531',
        'senderNickname': '群友甲',
        'senderGroupCard': '甲',
        'botName': '月璃',
        'text': '[转发消息]',
        'mentionedMe': False,
        'externalMessageId': '5001',
    }
    valid = {
        'nodes': [{
            'senderName': '内层用户',
            'parts': [{'kind': 'text', 'text': '正文'}],
        }],
    }

    body = PlatformInboundBody.model_validate({
        **base,
        'forwardMessages': [valid],
    })

    assert body.forward_messages == [valid]
    with pytest.raises(ValueError, match=r'forwardMessages\[0\].*nodes 不能为空'):
        PlatformInboundBody.model_validate({
            **base,
            'forwardMessages': [{'nodes': []}],
        })
