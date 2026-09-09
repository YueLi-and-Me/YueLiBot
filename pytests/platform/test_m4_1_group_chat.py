"""验证群聊白名单、入站解析、历史隔离和出站目标选择。

本模块覆盖群 ID 准入、群成员身份、群历史中的说话人、回复门控和群消息发送，
确保群聊不会绕过平台访问控制或复用私聊上下文。
"""

from __future__ import annotations

from base64 import b64encode
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, List

import sqlite3

import pytest

from src.platforms.onebot11.backend import BackendOutbound
from src.platforms.onebot11.config import (
    GroupAccessConfig,
    ProtocolConnectionConfig,
    AdapterDocument,
    PrivateAccessConfig,
)
from src.platforms.onebot11.events import classify_event, parse_inbound_event
import src.platforms.onebot11.runner as runner_module
from src.platforms.onebot11.runner import OneBot11Runner
from src.core.api.http import PlatformInboundBody, platform_inbound
from src.core.api.state import app_state
from src.core.config.schema import Config
from src.core.platform_io.registry import StreamRegistry
from src.core.services.chat import ChatService
from src.core.observe.store import event_store


NOW = 1_700_000_200_000


async def _noop(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    return None


def _group_payload(
    message_id: int,
    group_id: int,
    sender_id: int,
    text: str,
    *,
    mentioned: bool = False,
) -> dict[str, Any]:
    message: List[dict[str, Any]] = [{'type': 'text', 'data': {'text': text}}]
    if mentioned:
        message.append({'type': 'at', 'data': {'qq': '13579'}})
    return {
        'post_type': 'message',
        'message_type': 'group',
        'message_id': message_id,
        'group_id': group_id,
        'user_id': sender_id,
        'self_id': 13579,
        'message': message,
        'sender': {'user_id': sender_id, 'nickname': f'群友{sender_id}'},
    }


def _document(group: GroupAccessConfig) -> AdapterDocument:
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
        group=group,
    )


class _FiniteEventTransport:
    def __init__(self, payloads: List[dict[str, Any]]) -> None:
        self._payloads = payloads

    async def iter_events(self) -> AsyncIterator[dict[str, Any]]:
        for payload in self._payloads:
            yield payload


class _RecordingBackend:
    def __init__(self, outbound: List[BackendOutbound] | None = None) -> None:
        self.submitted: List[object] = []
        self._outbound = outbound or []

    async def submit_inbound(self, event: object) -> None:
        self.submitted.append(event)

    async def iter_outbound(self) -> AsyncIterator[BackendOutbound]:
        for message in self._outbound:
            yield message


class _RecordingActionTransport:
    def __init__(self) -> None:
        self.actions: List[tuple[str, dict[str, Any]]] = []

    async def call_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.actions.append((action, params))
        return {'status': 'ok', 'retcode': 0, 'data': {}}


class _ReplyProvider:
    def __init__(self) -> None:
        self.messages: List[dict[str, str]] = []

    async def stream(
        self,
        messages: List[dict[str, str]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        self.messages = messages
        yield {'text': '<say>在呢</say>'}


def test_group_whitelist_has_no_owner_exemption() -> None:
    """群准入只依据群 ID；即使发言人是 owner，名单外群也必须拒绝。"""
    access = GroupAccessConfig(mode='whitelist', list=['86420'])
    owner_in_denied_group = _group_payload(1, 99999, 24680, '我在名单外群')
    stranger_in_allowed_group = _group_payload(2, 86420, 97531, '我在名单内群')

    assert access.allows('86420') is True
    assert access.allows('99999') is False
    assert classify_event(
        owner_in_denied_group,
        '13579',
        '24680',
        PrivateAccessConfig(),
        access,
    ) == 'group_denied'
    assert classify_event(
        stranger_in_allowed_group,
        '13579',
        '24680',
        PrivateAccessConfig(),
        access,
    ) == 'message'


def test_group_access_is_strict_whitelist_with_empty_default() -> None:
    """群聊不提供 blacklist/allow-all 模式；默认空名单必须拒绝所有群。"""
    default_access = GroupAccessConfig()
    numeric_access = GroupAccessConfig(mode='whitelist', list=[86420])

    assert default_access.mode == 'whitelist'
    assert default_access.list == []
    assert default_access.allows('86420') is False
    assert numeric_access.list == ['86420']
    with pytest.raises(ValueError, match='whitelist'):
        GroupAccessConfig(mode='blacklist', list=[])


def test_group_sender_preserves_nickname_card_and_bot_login_name() -> None:
    """账号昵称与群名片不能互相覆盖；机器人登录昵称也要送到主体。"""
    payload = _group_payload(20, 86420, 97531, '月璃在吗')
    payload['sender'] = {
        'user_id': 97531,
        'nickname': '账号昵称',
        'card': '群名片',
    }

    event = parse_inbound_event(
        payload,
        '13579',
        '月璃QQ昵称',
        '24680',
        PrivateAccessConfig(),
        GroupAccessConfig(mode='whitelist', list=['86420']),
    )

    assert event is not None
    assert event.sender_nickname == '账号昵称'
    assert event.sender_group_card == '群名片'
    assert event.bot_name == '月璃QQ昵称'

    mentioned_payload = _group_payload(21, 86420, 97531, '叫你一下', mentioned=True)
    mentioned_event = parse_inbound_event(
        mentioned_payload,
        '13579',
        '月璃QQ昵称',
        '24680',
        PrivateAccessConfig(),
        GroupAccessConfig(mode='whitelist', list=['86420']),
    )
    assert mentioned_event is not None
    assert mentioned_event.text == '叫你一下@月璃QQ昵称'


def test_qq_number_is_stable_identity_while_names_follow_latest_event(
    db: sqlite3.Connection,
) -> None:
    """QQ 号负责去重，账号昵称和每个群的群名片都是可变属性。"""
    registry = StreamRegistry(db)
    first = registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='97531',
        sender_nickname='旧昵称',
        sender_group_card='旧群名片',
        first_seen_at=NOW,
    )
    second = registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='97531',
        sender_nickname='新昵称',
        sender_group_card='新群名片',
        first_seen_at=NOW + 1,
    )
    same_name_other_qq = registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='97532',
        sender_nickname='新昵称',
        sender_group_card='',
        first_seen_at=NOW + 2,
    )

    assert first.person.id == second.person.id
    assert same_name_other_qq.person.id != second.person.id
    assert registry.display_name(second.person.id, 'qq') == '新昵称'
    assert registry.stream_display_name(second.person.id, second.stream.id) == '新群名片'
    assert registry.group_memberships(second.person.id)[0].group_card == '新群名片'


async def test_denied_group_is_dropped_before_backend_submission() -> None:
    """名单外群消息在适配器侧丢弃，owner 发言也不得触达主体。"""
    backend = _RecordingBackend()
    runner = OneBot11Runner(
        _document(GroupAccessConfig(mode='whitelist', list=['86420'])),
        backend_port=1,
        token='backend-secret',
        transport=_FiniteEventTransport([
            _group_payload(3, 99999, 24680, '名单外群里的用户本人'),
        ]),
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert backend.submitted == []


async def test_allowed_group_submits_mentioned_and_unmentioned_messages() -> None:
    """白名单群的消息都进入主体，是否回复由主体 reply_gate 决定。"""
    backend = _RecordingBackend()
    runner = OneBot11Runner(
        _document(GroupAccessConfig(mode='whitelist', list=['86420'])),
        backend_port=1,
        token='backend-secret',
        transport=_FiniteEventTransport([
            _group_payload(4, 86420, 97531, '普通消息'),
            _group_payload(5, 86420, 97531, '叫你一下', mentioned=True),
        ]),
        backend=backend,
    )

    await runner._consume_protocol_events('13579', '月璃')

    assert len(backend.submitted) == 2
    assert [event.mentioned_me for event in backend.submitted] == [False, True]
    assert all(event.stream_kind == 'group' for event in backend.submitted)


async def test_unmentioned_group_message_is_stored_without_reply(
    db: sqlite3.Connection,
) -> None:
    """未提及角色的群消息原样落库并记录 reply_gate trace，但不进入回复模型。"""
    previous_chat = app_state.chat
    previous_registry = app_state.registry
    previous_register = app_state.register_platform_stream
    previous_group_chat = app_state.group_chat_config
    registry = StreamRegistry(db)
    config = Config()
    config.bot.name = '测试角色'
    chat = ChatService(db, None, None, None, _noop, cfg=config)
    before_trace = event_store.since(0).events
    since = before_trace[-1]['seq'] if before_trace else 0
    app_state.chat = chat
    app_state.registry = registry
    app_state.register_platform_stream = None
    app_state.group_chat_config = config.group_chat

    try:
        response = await platform_inbound(PlatformInboundBody(
            platform='qq',
            streamKind='group',
            streamExternalId='86420',
            senderExternalId='97531',
            senderNickname='账号昵称',
            senderGroupCard='小李',
            text='这句不带前缀',
            mentionedMe=False,
            externalMessageId='6',
        ))
    finally:
        app_state.chat = previous_chat
        app_state.registry = previous_registry
        app_state.register_platform_stream = previous_register
        app_state.group_chat_config = previous_group_chat

    payload = response.body.decode('utf-8')
    rows = [
        tuple(row)
        for row in db.execute(
            "SELECT role, content FROM messages WHERE stream_id = 2 ORDER BY id ASC"
        ).fetchall()
    ]
    gate_entries = [
        entry for entry in event_store.since(since).events
        if entry['kind'] == 'reply_gate' and entry['streamId'] == 2
    ]

    assert '"accepted":false' in payload
    assert '"reason":"attention_filtered"' in payload
    assert rows == [('user', '这句不带前缀')]
    assert chat._turn_id == 0
    assert gate_entries[-1]['reason'] == 'attention_filtered'
    assert gate_entries[-1]['disposition'] == 'drop'


@pytest.mark.parametrize('configured_name', ['Bot#7', '星', 'NOVA!'])
async def test_only_configured_name_or_alias_can_trigger_reply(
    db: sqlite3.Connection,
    configured_name: str,
) -> None:
    """名称门控只读取配置，且不限制已登记名称的文字或符号类型。"""
    previous_chat = app_state.chat
    previous_registry = app_state.registry
    previous_register = app_state.register_platform_stream
    previous_group_chat = app_state.group_chat_config
    config = Config()
    config.bot.name = 'Bot#7'
    config.bot.aliases = ['星', 'NOVA!']
    config.group_chat.name_mention_probability = 1.0
    provider = _ReplyProvider()
    registry = StreamRegistry(db)
    chat = ChatService(db, provider, None, None, _noop, cfg=config)
    app_state.chat = chat
    app_state.registry = registry
    app_state.register_platform_stream = None
    app_state.group_chat_config = config.group_chat

    try:
        response = await platform_inbound(PlatformInboundBody(
            platform='qq',
            streamKind='group',
            streamExternalId='86420',
            senderExternalId='97531',
            senderNickname='账号昵称',
            senderGroupCard='群名片',
            botName='未登记的平台登录昵称',
            text=f'x{configured_name.casefold()}y',
            mentionedMe=False,
            externalMessageId='21',
        ))
        await chat._tick()
        await chat._inflight[2].task
    finally:
        app_state.chat = previous_chat
        app_state.registry = previous_registry
        app_state.register_platform_stream = previous_register
        app_state.group_chat_config = previous_group_chat

    payload = response.body.decode('utf-8')
    system = provider.messages[0]['content']
    assert '"accepted":true' in payload
    assert '"reason":"name_mention"' in payload
    assert configured_name.casefold() in system.casefold()
    assert '未登记的平台登录昵称' in system


async def test_unconfigured_platform_login_name_does_not_trigger_reply(
    db: sqlite3.Connection,
) -> None:
    """协议登录昵称只用于消息上下文，未写入 bot.toml 时不能旁路名称门控。"""
    previous_chat = app_state.chat
    previous_registry = app_state.registry
    previous_register = app_state.register_platform_stream
    previous_group_chat = app_state.group_chat_config
    config = Config()
    config.bot.name = 'Bot#7'
    config.bot.aliases = ['星', 'NOVA!']
    provider = _ReplyProvider()
    registry = StreamRegistry(db)
    chat = ChatService(db, provider, None, None, _noop, cfg=config)
    app_state.chat = chat
    app_state.registry = registry
    app_state.register_platform_stream = None
    app_state.group_chat_config = config.group_chat

    try:
        response = await platform_inbound(PlatformInboundBody(
            platform='qq',
            streamKind='group',
            streamExternalId='86420',
            senderExternalId='97531',
            senderNickname='账号昵称',
            senderGroupCard='群名片',
            botName='未登记的平台登录昵称',
            text='未登记的平台登录昵称，在吗',
            mentionedMe=False,
            externalMessageId='22',
        ))
    finally:
        app_state.chat = previous_chat
        app_state.registry = previous_registry
        app_state.register_platform_stream = previous_register
        app_state.group_chat_config = previous_group_chat

    payload = response.body.decode('utf-8')
    assert '"accepted":false' in payload
    assert '"reason":"attention_filtered"' in payload
    assert provider.messages == []


def test_group_history_prefix_is_added_only_when_reading(
    db: sqlite3.Connection,
) -> None:
    """群历史能够辨认说话人，库内正文与私聊历史保持原样。"""
    registry = StreamRegistry(db)
    first = registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='10001',
        sender_nickname='小李',
        sender_group_card='小李',
        first_seen_at=NOW,
    )
    second = registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='10002',
        sender_nickname='小王',
        sender_group_card='小王',
        first_seen_at=NOW + 1,
    )
    direct = registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='10001',
        sender_external_id='10001',
        sender_nickname='小李',
        sender_group_card='',
        first_seen_at=NOW + 2,
    )
    chat = ChatService(db, None, None, None, _noop, cfg=Config())
    chat.memory.append_message(first.stream.id, first.person.id, 'user', '第一句', NOW)
    chat.memory.append_message(second.stream.id, second.person.id, 'user', '第二句', NOW + 1)
    chat.memory.append_message(direct.stream.id, direct.person.id, 'user', '私聊原句', NOW + 2)

    group_stored = chat.memory.working_memory(first.stream.id)
    direct_stored = chat.memory.working_memory(direct.stream.id)
    group_history = chat._history_for_context(first, group_stored)
    direct_history = chat._history_for_context(direct, direct_stored)
    stored_rows = [
        tuple(row)
        for row in db.execute(
            'SELECT content FROM messages WHERE stream_id = ? ORDER BY id ASC',
            (first.stream.id,),
        ).fetchall()
    ]

    # 用户历史行带发言时刻：当天首条带日期，同日后续只带时分。库内正文不受影响。
    first_stamp = datetime.fromtimestamp(NOW / 1000).strftime('%m-%d %H:%M')
    second_stamp = datetime.fromtimestamp((NOW + 1) / 1000).strftime('%H:%M')
    direct_stamp = datetime.fromtimestamp((NOW + 2) / 1000).strftime('%m-%d %H:%M')
    assert group_history == [
        {'role': 'user', 'content': f'{first_stamp} 小李: 第一句'},
        {'role': 'user', 'content': f'{second_stamp} 小王: 第二句'},
    ]
    assert stored_rows == [('第一句',), ('第二句',)]
    assert direct_history == [{'role': 'user', 'content': f'{direct_stamp} 私聊原句'}]


async def test_group_outbound_uses_send_group_msg_and_keeps_private_unchanged() -> None:
    """群出站使用 group_id，私聊继续使用 user_id。"""
    backend = _RecordingBackend(outbound=[
        BackendOutbound(
            stream_id=2,
            stream_kind='group',
            stream_external_id='86420',
            segments=['群里', '回复'],
        ),
        BackendOutbound(
            stream_id=3,
            stream_kind='direct',
            stream_external_id='24680',
            segments=['私聊', '回复'],
        ),
    ])
    transport = _RecordingActionTransport()
    runner = OneBot11Runner(
        _document(GroupAccessConfig(mode='whitelist', list=['86420'])),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_backend_outbound()

    # 每条分句各发一次 action：拼接会让整轮回复在 QQ 里挤成一个气泡。
    assert transport.actions == [
        ('send_group_msg', {
            'group_id': 86420,
            'message': [{'type': 'text', 'data': {'text': '群里'}}],
        }),
        ('send_group_msg', {
            'group_id': 86420,
            'message': [{'type': 'text', 'data': {'text': '回复'}}],
        }),
        ('send_private_msg', {
            'user_id': 24680,
            'message': [{'type': 'text', 'data': {'text': '私聊'}}],
        }),
        ('send_private_msg', {
            'user_id': 24680,
            'message': [{'type': 'text', 'data': {'text': '回复'}}],
        }),
    ]


@pytest.mark.asyncio
async def test_bubbles_wait_for_typing_time_between_sends(monkeypatch) -> None:
    """适配器按主体下发的停顿逐条等待，首条立即发出。"""
    waits: List[float] = []

    async def _record_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(runner_module.asyncio, 'sleep', _record_sleep)
    backend = _RecordingBackend(outbound=[BackendOutbound(
        stream_id=2,
        stream_kind='group',
        stream_external_id='86420',
        segments=['在呢', '你说你说，我听着呢'],
        batch_delays_ms=(0, 3_100),
    )])
    transport = _RecordingActionTransport()
    runner = OneBot11Runner(
        _document(GroupAccessConfig(mode='whitelist', list=['86420'])),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_backend_outbound()

    assert len(transport.actions) == 2
    # 首条不等：模型生成已经占了十几秒，再等一次就是明显的迟钝。
    # 节奏由主体算好下发，适配器只照做，不自行重算打字速度。
    assert waits == [3.1]


@pytest.mark.asyncio
async def test_text_and_emoji_are_sent_as_separate_qq_messages(tmp_path: Path) -> None:
    """文字与表情包分别调用一次 action，QQ 不再渲染为混合消息气泡。"""

    image_path = tmp_path / '表情.gif'
    content = b'GIF89a'
    image_path.write_bytes(content)
    backend = _RecordingBackend(outbound=[BackendOutbound(
        stream_id=2,
        stream_kind='group',
        stream_external_id='86420',
        segments=['先看这张'],
        emoji_refs=(image_path.as_uri(),),
        emoji_sub_types=(1,),
    )])
    transport = _RecordingActionTransport()
    runner = OneBot11Runner(
        _document(GroupAccessConfig(mode='whitelist', list=['86420'])),
        backend_port=1,
        token='backend-secret',
        transport=transport,
        backend=backend,
    )

    await runner._consume_backend_outbound()

    assert transport.actions == [
        ('send_group_msg', {
            'group_id': 86420,
            'message': [{'type': 'text', 'data': {'text': '先看这张'}}],
        }),
        ('send_group_msg', {
            'group_id': 86420,
            'message': [{
                'type': 'image',
                'data': {
                    'file': 'base64://' + b64encode(content).decode('ascii'),
                    'sub_type': 1,
                },
            }],
        }),
    ]
