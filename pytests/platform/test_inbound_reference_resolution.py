"""验证 QQ 入站消息里的引用与提及在进入主体前被还原为可读文本。

覆盖 `src/platforms/onebot11/segments.py` 的引用摘要渲染、客户端自动补入的
提及丢弃，以及 `src/platforms/onebot11/runner.py` 通过协议端还原被引用消息原文和
被提及者显示名的流程；协议端交互用假 transport 替身模拟，不建立真实连接。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from src.platforms.onebot11.config import (
    GroupAccessConfig,
    ProtocolConnectionConfig,
    AdapterDocument,
    OwnerConfig,
    PrivateAccessConfig,
)
from src.platforms.onebot11.runner import OneBot11Runner
from src.platforms.onebot11.segments import (
    FORWARD_PLACEHOLDER,
    mentioned_user_ids,
    message_to_text,
    quoted_message_ids,
)


def _document(group_ids: List[str]) -> AdapterDocument:
    """构造只开放指定群号的适配器配置。"""
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
        group=GroupAccessConfig(mode='whitelist', list=group_ids),
    )


class _RecordingTransport:
    """按 action 名返回预设响应，并记录全部调用顺序的传输替身。"""

    def __init__(
        self,
        payloads: List[Dict[str, Any]],
        responses: Dict[str, Any],
    ) -> None:
        self._payloads = payloads
        self._responses = responses
        self.actions: List[tuple[str, Dict[str, Any]]] = []

    async def iter_events(self):
        for payload in self._payloads:
            yield payload

    async def call_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.actions.append((action, params))
        if action not in self._responses:
            raise AssertionError(f'未预期的 action：{action}')
        response = self._responses[action]
        if isinstance(response, Exception):
            raise response
        return response


class _CollectingBackend:
    """只收集入站事件的主体客户端替身。"""

    def __init__(self) -> None:
        self.submitted: List[Any] = []

    async def submit_inbound(self, event: Any) -> None:
        self.submitted.append(event)


def _group_payload(segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """构造一条来自白名单群的普通群消息事件。"""
    return {
        'post_type': 'message',
        'message_type': 'group',
        'message_id': 5001,
        'group_id': 86420,
        'user_id': 97531,
        'self_id': 13579,
        'message': segments,
        'sender': {'user_id': 97531, 'nickname': '群友甲', 'card': '甲'},
    }


def test_quote_preview_replaces_content_free_placeholder() -> None:
    """命中摘要映射时引用段渲染为带发送者和原文的可读文本。"""
    text = message_to_text(
        [
            {'type': 'reply', 'data': {'id': '4173'}},
            {'type': 'text', 'data': {'text': '？'}},
        ],
        {},
        {'4173': '回复 凌白：谁家好人没事打句号'},
    )

    assert text == '[回复 凌白：谁家好人没事打句号]？'


def test_unresolved_quote_keeps_placeholder() -> None:
    """摘要缺失时保留占位符，不臆造被引用内容。"""
    text = message_to_text(
        [
            {'type': 'reply', 'data': {'id': '4173'}},
            {'type': 'text', 'data': {'text': '？'}},
        ],
        {},
        {},
    )

    assert text == '[引用消息]？'


def test_auto_inserted_mention_after_quote_is_dropped() -> None:
    """紧跟引用段的提及由客户端自动补入，渲染时丢弃以避免重复称呼。"""
    text = message_to_text(
        [
            {'type': 'reply', 'data': {'id': '4173'}},
            {'type': 'at', 'data': {'qq': '900000001'}},
            {'type': 'text', 'data': {'text': ' 666'}},
        ],
        {'900000001': '凌白'},
        {'4173': '回复 凌白：谁家好人没事打句号'},
    )

    assert text == '[回复 凌白：谁家好人没事打句号] 666'


def test_standalone_mention_is_rendered_with_display_name() -> None:
    """非引用场景的提及照常渲染，并使用解析出的显示名。"""
    text = message_to_text(
        [
            {'type': 'at', 'data': {'qq': '1624606785'}},
            {'type': 'text', 'data': {'text': ' 我饿了'}},
        ],
        {'1624606785': '波波波波波奇'},
        {},
    )

    assert text == '@波波波波波奇 我饿了'


def test_reference_extractors_deduplicate_in_arrival_order() -> None:
    """提及与引用 ID 提取按出现顺序去重，@全体成员 不计入具体号码。"""
    segments = [
        {'type': 'reply', 'data': {'id': '11'}},
        {'type': 'at', 'data': {'qq': '33'}},
        {'type': 'at', 'data': {'qq': 'all'}},
        {'type': 'at', 'data': {'qq': '22'}},
        {'type': 'at', 'data': {'qq': '22'}},
        {'type': 'reply', 'data': {'id': '11'}},
    ]

    # 33 紧跟引用段，属于客户端自动补入的提及，渲染和解析都要跳过。
    assert mentioned_user_ids(segments) == ('22',)
    assert quoted_message_ids(segments) == ('11',)


@pytest.mark.asyncio
async def test_runner_restores_quote_content_and_mention_name() -> None:
    """运行器在提交入站前把引用原文和被提及者显示名补进正文。"""
    payload = _group_payload([
        {'type': 'reply', 'data': {'id': '4173'}},
        {'type': 'at', 'data': {'qq': '900000001'}},
        {'type': 'text', 'data': {'text': ' 666'}},
    ])
    transport = _RecordingTransport(
        [payload],
        {
            'get_msg': {
                'data': {
                    'message_id': 4173,
                    'sender': {'user_id': 900000001, 'nickname': '凌白'},
                    'message': [{'type': 'text', 'data': {'text': '谁家好人没事打句号'}}],
                },
            },
        },
    )
    backend = _CollectingBackend()
    runner = OneBot11Runner(
        _document(['86420']),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert len(backend.submitted) == 1
    assert backend.submitted[0].text == '[回复 凌白：谁家好人没事打句号] 666'
    # 被提及者的显示名已由引用还原顺带写入缓存，不必再查一次成员资料。
    assert [action for action, _ in transport.actions] == ['get_msg']


@pytest.mark.asyncio
async def test_runner_queries_member_info_for_unknown_mention() -> None:
    """未见过的被提及者向协议端查一次群名片，结果进入缓存供后续复用。"""
    payloads = [
        _group_payload([
            {'type': 'at', 'data': {'qq': '1624606785'}},
            {'type': 'text', 'data': {'text': ' 我饿了'}},
        ]),
        _group_payload([
            {'type': 'at', 'data': {'qq': '1624606785'}},
            {'type': 'text', 'data': {'text': ' 再问一次'}},
        ]),
    ]
    transport = _RecordingTransport(
        payloads,
        {
            'get_group_member_info': {
                'data': {'user_id': 1624606785, 'nickname': '波波奇', 'card': '波波波波波奇'},
            },
        },
    )
    backend = _CollectingBackend()
    runner = OneBot11Runner(
        _document(['86420']),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert [event.text for event in backend.submitted] == [
        '@波波波波波奇 我饿了',
        '@波波波波波奇 再问一次',
    ]
    assert transport.actions == [
        ('get_group_member_info', {'user_id': '1624606785', 'group_id': '86420'}),
    ]


@pytest.mark.asyncio
async def test_failed_reference_resolution_still_submits_message() -> None:
    """引用还原失败只丢摘要，消息本身照常入站，正文退回占位符。"""
    payload = _group_payload([
        {'type': 'reply', 'data': {'id': '4173'}},
        {'type': 'text', 'data': {'text': '？'}},
    ])
    transport = _RecordingTransport(
        [payload],
        {'get_msg': RuntimeError('消息已撤回')},
    )
    backend = _CollectingBackend()
    runner = OneBot11Runner(
        _document(['86420']),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert len(backend.submitted) == 1
    assert backend.submitted[0].text == '[引用消息]？'


@pytest.mark.asyncio
async def test_quote_of_bot_message_uses_bot_display_name() -> None:
    """引用她自己的消息时摘要用机器人显示名，而不是登录 QQ 号。"""
    payload = _group_payload([
        {'type': 'reply', 'data': {'id': '4177'}},
        {'type': 'text', 'data': {'text': '有一会儿了（）'}},
    ])
    transport = _RecordingTransport(
        [payload],
        {
            'get_msg': {
                'data': {
                    'message_id': 4177,
                    'sender': {'user_id': 13579, 'nickname': '这个昵称不该出现'},
                    'message': [{'type': 'text', 'data': {'text': '他多久没冒泡了'}}],
                },
            },
        },
    )
    backend = _CollectingBackend()
    runner = OneBot11Runner(
        _document(['86420']),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert backend.submitted[0].text == '[回复 月璃：他多久没冒泡了]有一会儿了（）'


async def test_quote_preview_neutralizes_forward_placeholder() -> None:
    """引用摘要不得原样带出转发占位，否则会和本条消息自己的占位混在一起。

    转发解析完成后要按位置把第 i 个转发占位替换成第 i 个根的结果；摘要里混进
    同形占位会让个数对不上，按位置的映射整个失效。摘要是对另一条消息的转述，
    那条转发能不能读由它自己那行回答，这里不需要保留可被工具读取的占位形态。
    """
    payload = _group_payload([
        {'type': 'reply', 'data': {'id': '4173'}},
        {'type': 'text', 'data': {'text': '这转的啥'}},
    ])
    transport = _RecordingTransport(
        [payload],
        {
            'get_msg': {
                'data': {
                    'message_id': 4173,
                    'sender': {'user_id': 900000001, 'nickname': '凌白'},
                    'message': [{'type': 'forward', 'data': {'id': 'res-1'}}],
                },
            },
        },
    )
    backend = _CollectingBackend()
    runner = OneBot11Runner(
        _document(['86420']),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    text = backend.submitted[0].text
    assert text == '[回复 凌白：[合并转发]]这转的啥'
    assert FORWARD_PLACEHOLDER not in text
