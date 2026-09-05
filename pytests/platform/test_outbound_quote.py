"""验证群聊回复在落地时挂上引用，让旁观者看得出她在接哪一条。

覆盖 v11→v12 迁移与平台消息编号落库、`ChatService._quote_target` 的判定口径、
QQ 驱动出站载荷字段，以及适配器把引用段挂在第一个发送批次上的组装规则。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import sqlite3

import pytest

from src.core.common.db.migrations.bootstrap import write_user_version
from src.core.common.db.migrations.manager import CURRENT_VERSION, run_migrations
from src.core.config.schema import Config
from src.core.memory.store import MemoryStore
from src.core.platform_io.drivers.qq_ws import QqWebSocketDriver
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import (
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
    StreamRef,
)
from src.core.services.chat import ChatService
from src.platforms.onebot11.backend import _parse_outbound
from src.platforms.onebot11.segments import outbound_message_batches


GROUP_STREAM_ID = 2
PERSON_ID = 2
NOW = 1_700_000_000_000


def _memory() -> MemoryStore:
    """建一个带 QQ 群 stream 和一个说话人物的内存库。"""
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    store = MemoryStore(db)
    store._db.execute(
        "INSERT INTO streams (id, platform, kind, external_id) VALUES (?, 'qq', 'group', '629201002')",
        (GROUP_STREAM_ID,),
    )
    store._db.execute(
        "INSERT INTO persons (id, kind, first_seen_at) VALUES (?, 'contact', ?)",
        (PERSON_ID, NOW),
    )
    store._db.commit()
    return store


def test_migration_adds_external_message_id_to_existing_database(tmp_path: Path) -> None:
    """已有库补上平台消息编号列，历史消息保持 NULL 而不是被填上假值。"""
    path = tmp_path / 'memory.db'
    db = sqlite3.connect(str(path))
    run_migrations(db, path)
    db.execute(
        "INSERT INTO streams (id, platform, kind, external_id) VALUES (?, 'qq', 'group', '1')",
        (GROUP_STREAM_ID,),
    )
    db.execute(
        "INSERT INTO persons (id, kind, first_seen_at) VALUES (?, 'contact', ?)",
        (PERSON_ID, NOW),
    )
    db.execute(
        'INSERT INTO messages (id, stream_id, sender_person_id, role, content, created_at) '
        "VALUES (900, ?, ?, 'user', '历史消息', ?)",
        (GROUP_STREAM_ID, PERSON_ID, NOW),
    )
    write_user_version(db, CURRENT_VERSION - 1)
    db.commit()
    db.close()

    db = sqlite3.connect(str(path))
    run_migrations(db, path)
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info('messages')")}
    legacy = db.execute('SELECT external_message_id FROM messages WHERE id = 900').fetchone()
    db.close()

    assert 'external_message_id' in columns
    assert legacy[0] is None


def test_append_message_round_trips_platform_message_id() -> None:
    """入站消息的平台编号落库，并能按 stream 隔离地读回。"""
    store = _memory()

    message_id = store.append_message(
        GROUP_STREAM_ID, PERSON_ID, 'user', '咱俩试试？', NOW, '2145541855',
    )

    assert store.external_message_id(GROUP_STREAM_ID, message_id) == '2145541855'
    # 另一个 stream 不该读到同一条消息的编号。
    assert store.external_message_id(GROUP_STREAM_ID + 1, message_id) is None


def test_has_user_messages_after_ignores_own_replies() -> None:
    """只有别人的发言算作冲开；她自己的回复不能让判据恒真。"""
    store = _memory()
    first = store.append_message(GROUP_STREAM_ID, PERSON_ID, 'user', '第一条', NOW, '1')

    assert store.has_user_messages_after(GROUP_STREAM_ID, first) is False

    store.append_message(GROUP_STREAM_ID, None, 'assistant', '<say>接住了</say>', NOW + 1)

    assert store.has_user_messages_after(GROUP_STREAM_ID, first) is False

    store.append_message(GROUP_STREAM_ID, PERSON_ID, 'user', '第二条', NOW + 2, '2')

    assert store.has_user_messages_after(GROUP_STREAM_ID, first) is True


def test_outbound_batches_quote_only_the_first_bubble() -> None:
    """引用段挂在第一个批次最前，后续气泡不重复挂引用。"""
    batches = outbound_message_batches(['第一句', '第二句'], (), (), '2145541855')

    assert batches[0] == [
        {'type': 'reply', 'data': {'id': '2145541855'}},
        {'type': 'text', 'data': {'text': '第一句'}},
    ]
    assert batches[1] == [{'type': 'text', 'data': {'text': '第二句'}}]


def test_outbound_batches_without_quote_are_unchanged() -> None:
    """不引用时批次结构与改动前一致。"""
    assert outbound_message_batches(['单句'], (), (), '') == [
        [{'type': 'text', 'data': {'text': '单句'}}],
    ]


@pytest.mark.asyncio
async def test_qq_driver_forwards_quote_to_adapter() -> None:
    """驱动把引用编号透传到 qq.send 载荷；不引用时不写该字段。"""
    sent: list[Dict[str, Any]] = []

    async def push(stream_id: int, event: str, payload: Any) -> int:
        sent.append(payload)
        return 1

    driver = QqWebSocketDriver(push)
    stream = StreamRef(id=GROUP_STREAM_ID, platform='qq', kind='group', external_id='629201002')

    await driver.send(OutboundMessage(
        stream=stream,
        segments=['接住了'],
        quote_external_message_id='2145541855',
    ))
    await driver.send(OutboundMessage(stream=stream, segments=['不引用']))

    assert sent[0]['quoteExternalMessageId'] == '2145541855'
    assert 'quoteExternalMessageId' not in sent[1]


def test_adapter_parses_quote_field() -> None:
    """适配器解析引用编号并去除空白；缺省时为空字符串。"""
    body = {
        'streamKind': 'group',
        'streamExternalId': '629201002',
        'segments': ['接住了'],
    }

    with_quote = _parse_outbound({
        'stream_id': GROUP_STREAM_ID,
        'payload': {**body, 'quoteExternalMessageId': ' 2145541855 '},
    })
    without_quote = _parse_outbound({'stream_id': GROUP_STREAM_ID, 'payload': body})

    assert with_quote.quote_external_message_id == '2145541855'
    assert without_quote.quote_external_message_id == ''


class _ScriptedProvider:
    """按调用顺序轮换分片脚本的替身提供方。"""

    def __init__(self, scripts: list[list[str]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, **_kwargs: Any) -> Any:
        self.calls += 1
        script = self.scripts[min(self.calls, len(self.scripts)) - 1]
        for text in script:
            yield {'text': text}


class _RecordingBroker:
    """记录出站消息并返回成功回执的 broker 替身。"""

    def __init__(self) -> None:
        self.dispatched: list[OutboundMessage] = []

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        self.dispatched.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=[],
        )


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


def _agent_config() -> Config:
    """启用 Conversation Agent 的最小配置。"""
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    return config


def _group_context(registry: StreamRegistry, sender_external_id: str = '97531'):
    """解析出一个 QQ 群聊上下文。"""
    return registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id=sender_external_id,
        sender_nickname='群友',
        sender_group_card='群友',
        first_seen_at=NOW,
    )


async def test_group_reply_quotes_target_once_others_have_spoken(db) -> None:
    """目标之后已经有人插话时，群聊回复挂上目标消息的平台编号。"""
    provider = _ScriptedProvider([[
        '<decision action="reply" targets="{target}" reasons="natural_reaction" length="brief"/>',
        '<say emotion="smile">接住了</say>',
    ]])
    broker = _RecordingBroker()
    chat = ChatService(db, provider, None, None, _noop, cfg=_agent_config(), broker=broker)
    context = _group_context(chat._registry)

    await chat.send(InboundMessage(
        text='@月璃 咱俩试试？',
        context=context,
        mentioned_me=True,
        external_message_id='2145541855',
    ))
    target_id = chat._buffers[context.stream.id][0].message_id
    provider.scripts[0][0] = provider.scripts[0][0].format(target=target_id)
    # 生成期间别人插话：回复落地时目标已被冲开，必须靠引用点明在接哪一条。
    other = _group_context(chat._registry, sender_external_id='97532')
    await chat.send(InboundMessage(
        text='笑死',
        context=other,
        external_message_id='2145541856',
    ))

    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert len(broker.dispatched) == 1
    assert broker.dispatched[0].quote_external_message_id == '2145541855'


async def test_group_reply_without_displacement_is_not_quoted(db) -> None:
    """目标就是本 stream 最新一条时不挂引用，避免在安静的群里刷引用框。"""
    provider = _ScriptedProvider([[
        '<decision action="reply" targets="{target}" reasons="natural_reaction" length="brief"/>',
        '<say emotion="smile">接住了</say>',
    ]])
    broker = _RecordingBroker()
    chat = ChatService(db, provider, None, None, _noop, cfg=_agent_config(), broker=broker)
    context = _group_context(chat._registry)

    await chat.send(InboundMessage(
        text='@月璃 在吗',
        context=context,
        mentioned_me=True,
        external_message_id='2145541855',
    ))
    target_id = chat._buffers[context.stream.id][0].message_id
    provider.scripts[0][0] = provider.scripts[0][0].format(target=target_id)

    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert len(broker.dispatched) == 1
    assert broker.dispatched[0].quote_external_message_id is None


async def test_observation_only_message_also_stores_platform_id(db) -> None:
    """被门控拒绝、只观察不回复的消息同样保留平台编号。

    这些消息占群里绝大多数；不存编号的话，一旦她随后接的是其中一条，
    投递期就还原不出平台编号，引用挂不上。
    """
    chat = ChatService(db, None, None, None, _noop, cfg=_agent_config())
    context = _group_context(chat._registry)

    message_id = chat.record_group_observation(InboundMessage(
        text='别玩大禹了',
        context=context,
        external_message_id='2145541857',
    ))

    assert chat.memory.external_message_id(context.stream.id, message_id) == '2145541857'
