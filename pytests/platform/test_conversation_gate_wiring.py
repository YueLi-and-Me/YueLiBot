"""Conversation 行动核心入口门控接线验收。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import json
import sqlite3

from src.core.api.http import (
    GroupBackfillBody,
    GroupBackfillMessageBody,
    PlatformInboundBody,
    platform_group_backfill,
    platform_inbound,
)
from src.core.api.state import app_state
from src.core.runtime.clock import now as current_time
from src.core.config.schema import Config
from src.core.observe.store import event_store
from src.core.platform_io.registry import StreamRegistry
from src.core.services.chat import ChatService


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


class _NoopProvider:
    async def stream(self, **_kwargs: Any):
        if False:
            yield {'text': ''}


def _body(text: str, **overrides: Any) -> PlatformInboundBody:
    base: dict[str, Any] = dict(
        platform='qq',
        streamKind='group',
        streamExternalId='86420',
        senderExternalId='97531',
        senderNickname='账号昵称',
        senderGroupCard='小李',
        text=text,
        mentionedMe=False,
        externalMessageId='6',
    )
    base.update(overrides)
    return PlatformInboundBody(**base)


def _install(
    db: sqlite3.Connection,
    config: Config,
    provider: Any = None,
) -> tuple[ChatService, Any, Any, Any, Any]:
    """把测试聊天服务装配进全局状态，返回用于恢复的旧状态。"""
    previous = (
        app_state.chat,
        app_state.registry,
        app_state.register_platform_stream,
        app_state.group_chat_config,
    )
    registry = StreamRegistry(db)
    chat = ChatService(db, provider, provider, provider, _noop, cfg=config)
    app_state.chat = chat
    app_state.registry = registry
    app_state.register_platform_stream = None
    app_state.group_chat_config = config.group_chat
    return (chat, *previous)


async def test_unmentioned_group_message_drops_with_auditable_event(db: sqlite3.Connection) -> None:
    """无注意力信号的群消息 DROP：不建回合、不调模型，落 gate_dropped 四层事件。"""
    config = Config()
    config.bot.name = '月璃'
    chat, prev_chat, prev_registry, prev_register, prev_group_chat = _install(db, config)
    try:
        response = await platform_inbound(_body("这句不带前缀"))
    finally:
        app_state.chat = prev_chat
        app_state.registry = prev_registry
        app_state.register_platform_stream = prev_register
        app_state.group_chat_config = prev_group_chat

    payload = response.body.decode("utf-8")
    assert '"accepted":false' in payload
    assert '"reason":"attention_filtered"' in payload
    assert chat._turn_id == 0

    gate_events = [
        entry for entry in event_store.search(kinds=["action_decision"]).events
        if entry['eventStatus'] == 'gate_dropped'
    ]
    assert len(gate_events) == 1
    entry = gate_events[0]
    assert entry['turnId'] is None
    assert entry['gate']['disposition'] == 'drop'
    assert entry['gate']['reasonCodes'] == ['attention_filtered']
    assert entry['gate']['availableActions'] == []
    assert entry['decision'] is None
    assert entry['inputs']['streamKind'] == 'group'
    assert entry['inputs']['mentionedMe'] is False
    assert entry['inputs']['nameMentioned'] is False
    assert entry['inputs']['asleep'] is False
    assert entry['inputs']['rateLimited'] is False


async def test_frequency_trigger_defers_plain_group_message_to_chat(db: sqlite3.Connection) -> None:
    """frequency 口径：无信号群消息不再在 HTTP 入口丢弃，留给聊天服务累计预算。"""
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'shadow'
    config.conversation_agent.trigger_mode = 'frequency'
    provider = _NoopProvider()
    chat, prev_chat, prev_registry, prev_register, prev_group_chat = _install(
        db, config, provider,
    )
    try:
        response = await platform_inbound(_body("一条普通群消息"))
    finally:
        app_state.chat = prev_chat
        app_state.registry = prev_registry
        app_state.register_platform_stream = prev_register
        app_state.group_chat_config = prev_group_chat

    payload = response.body.decode("utf-8")
    assert '"accepted":true' in payload
    assert '"reason":"deferred_to_trigger_mode"' in payload
    assert len(chat._buffers) == 1
    gates = event_store.search(kinds=["reply_gate"]).events
    assert gates[-1]['disposition'] == 'drop'
    assert gates[-1]['reasonCodes'] == ['attention_filtered']


async def test_name_mention_enters_deliberate_and_buffers(db: sqlite3.Connection) -> None:
    """名字出现进入 DELIBERATE：消息入缓冲，是否回复不在此处写死。"""
    config = Config()
    config.bot.name = '月璃'
    chat, prev_chat, prev_registry, prev_register, prev_group_chat = _install(db, config)
    try:
        response = await platform_inbound(_body("月璃，在吗"))
    finally:
        app_state.chat = prev_chat
        app_state.registry = prev_registry
        app_state.register_platform_stream = prev_register
        app_state.group_chat_config = prev_group_chat

    payload = response.body.decode("utf-8")
    assert '"accepted":true' in payload
    assert '"reason":"name_mention"' in payload
    assert chat._turn_id == 0  # 只入缓冲，回合由聊天循环创建
    assert len(chat._buffers) == 1
    gates = event_store.search(kinds=["reply_gate"]).events
    assert gates[-1]['disposition'] == 'deliberate'
    assert gates[-1]['reasonCodes'] == ['name_mention']


async def test_poke_skips_synthetic_name_and_over_limit_arrival_drops(
    db: sqlite3.Connection,
) -> None:
    """合成正文不算名字点名；同一信号窗口内超出上限的 poke 以重复原因丢弃。"""
    config = Config()
    config.bot.name = '月璃'
    chat, prev_chat, prev_registry, prev_register, prev_group_chat = _install(db, config)
    responses = []
    try:
        for index in range(4):
            responses.append(await platform_inbound(_body(
                '[揉了揉月璃]',
                pokedMe=True,
                externalMessageId=f'poke-{index}',
            )))
    finally:
        app_state.chat = prev_chat
        app_state.registry = prev_registry
        app_state.register_platform_stream = prev_register
        app_state.group_chat_config = prev_group_chat

    payloads = [json.loads(response.body) for response in responses]
    # 窗口内前 POKE_SIGNAL_LIMIT 次照常唤起回合，超出的直接丢弃。
    assert [payload['accepted'] for payload in payloads] == [True, True, True, False]
    assert [payload['reason'] for payload in payloads[:3]] == ['direct_poke'] * 3
    assert payloads[3]['reason'] == 'poke_repeat'

    gates = list(reversed(event_store.search(kinds=['reply_gate']).events))[-4:]
    assert all(gate['nameMentioned'] is False for gate in gates)
    assert [gate['pokesInWindow'] for gate in gates] == [1, 2, 3, 4]
    assert [gate['disposition'] for gate in gates[:3]] == ['deliberate'] * 3
    assert gates[3]['disposition'] == 'drop'
    assert gates[3]['reasonCodes'] == ['poke_repeat']


async def test_at_mention_must_reply_is_forced(db: sqlite3.Connection) -> None:
    """真实 @ 且 @必回开启时 FORCE，动作集由运行时给出。"""
    config = Config()
    config.bot.name = '月璃'
    chat, prev_chat, prev_registry, prev_register, prev_group_chat = _install(db, config)
    try:
        response = await platform_inbound(_body("@月璃 在吗", mentionedMe=True))
    finally:
        app_state.chat = prev_chat
        app_state.registry = prev_registry
        app_state.register_platform_stream = prev_register
        app_state.group_chat_config = prev_group_chat

    payload = response.body.decode("utf-8")
    assert '"accepted":true' in payload
    assert '"reason":"at_mention_must_reply"' in payload
    gates = event_store.search(kinds=["reply_gate"]).events
    assert gates[-1]['disposition'] == 'force'
    assert gates[-1]['reasonCodes'] == ['at_mention_must_reply']


async def test_asleep_group_message_drops_with_asleep_fact(db: sqlite3.Connection) -> None:
    """休眠群消息 DROP，审计事件里 asleep 事实为真。"""
    config = Config()
    config.bot.name = '月璃'
    chat, prev_chat, prev_registry, prev_register, prev_group_chat = _install(db, config)
    chat.set_sleep_state_provider(
        lambda: SimpleNamespace(asleep=True, just_woke=False, resting=False)
    )
    try:
        response = await platform_inbound(_body("月璃，在吗"))
    finally:
        app_state.chat = prev_chat
        app_state.registry = prev_registry
        app_state.register_platform_stream = prev_register
        app_state.group_chat_config = prev_group_chat

    payload = response.body.decode("utf-8")
    assert '"accepted":false' in payload
    assert '"reason":"asleep"' in payload
    gate_events = [
        entry for entry in event_store.search(kinds=["action_decision"]).events
        if entry['eventStatus'] == 'gate_dropped'
    ]
    assert gate_events[-1]['inputs']['asleep'] is True


async def test_rate_limited_group_message_drops(db: sqlite3.Connection) -> None:
    """窗口内回复数达到硬上限时，无人点名的普通群消息 DROP。

    上限只约束「没人点名的自发参与」；有人叫她名字时的放行由
    ``test_rate_limited_group_still_answers_when_named`` 覆盖。
    """
    config = Config()
    config.bot.name = '月璃'
    chat, prev_chat, prev_registry, prev_register, prev_group_chat = _install(db, config)
    registry: StreamRegistry = app_state.registry
    context = registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='小李',
        first_seen_at=1_000_000,
    )
    for index in range(config.group_chat.max_replies_in_window):
        chat.memory.append_message(
            context.stream.id,
            None,
            'assistant',
            f'回复{index}',
            current_time() - index,
        )
    try:
        response = await platform_inbound(_body("今天天气不错"))
    finally:
        app_state.chat = prev_chat
        app_state.registry = prev_registry
        app_state.register_platform_stream = prev_register
        app_state.group_chat_config = prev_group_chat

    payload = response.body.decode("utf-8")
    assert '"accepted":false' in payload
    assert '"reason":"rate_limited"' in payload
    gates = event_store.search(kinds=["reply_gate"]).events
    assert gates[-1]['repliesInWindow'] == config.group_chat.max_replies_in_window


async def test_rate_limited_group_still_answers_when_named(
    db: sqlite3.Connection,
) -> None:
    """撞上频率硬上限，但有人直接叫她名字时仍然放行。

    上限的判据是她自己说了多少，与「这句话是不是冲着她来的」无关。真机上出现过
    「小璃你要为我做主啊」这类明确点名被上限压掉、她完全没有反应的情况。放行后
    仍只是 DELIBERATE，她自己可以选择沉默。
    """
    config = Config()
    config.bot.name = '月璃'
    chat, prev_chat, prev_registry, prev_register, prev_group_chat = _install(db, config)
    registry: StreamRegistry = app_state.registry
    context = registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='小李',
        first_seen_at=1_000_000,
    )
    for index in range(config.group_chat.max_replies_in_window):
        chat.memory.append_message(
            context.stream.id,
            None,
            'assistant',
            f'回复{index}',
            current_time() - index,
        )
    try:
        response = await platform_inbound(_body("月璃，在吗"))
    finally:
        app_state.chat = prev_chat
        app_state.registry = prev_registry
        app_state.register_platform_stream = prev_register
        app_state.group_chat_config = prev_group_chat

    payload = response.body.decode("utf-8")
    assert '"accepted":true' in payload
    assert '"reason":"name_mention"' in payload

async def test_inbound_images_are_described_before_chat_buffer(
    db: sqlite3.Connection,
    monkeypatch,
) -> None:
    """通过门控的普通图片在入站时合并 VLM 描述，失败项保留占位符。"""
    import base64

    config = Config()
    config.bot.name = '月璃'
    chat, prev_chat, prev_registry, prev_register, prev_group_chat = _install(
        db, config,
    )
    async def _describe(attachments: list[dict[str, Any]]) -> list[str | None]:
        assert len(attachments) == 1
        assert base64.b64decode(attachments[0]['data']) == b''
        return ['一只猫']

    monkeypatch.setattr(chat, 'describe_inbound_images', _describe)
    try:
        response = await platform_inbound(_body(
            '月璃看[图片]',
            imageSegments=[{'sha256': 'a', 'data': 'AQ==', 'mime': 'image/jpeg'}],
        ))
    finally:
        app_state.chat = prev_chat
        app_state.registry = prev_registry
        app_state.register_platform_stream = prev_register
        app_state.group_chat_config = prev_group_chat

    payload = json.loads(response.body)
    stored = [message.content for message in chat.memory.working_memory(payload['streamId'], 20)]
    assert any('[图片：一只猫]' in content for content in stored)



async def test_inbound_image_sources_are_described_in_background(
    db: sqlite3.Connection,
    monkeypatch,
) -> None:
    """新 imageSources 协议先落占位符，后台补齐描述后回写消息正文。"""
    config = Config()
    config.bot.name = '月璃'
    chat, prev_chat, prev_registry, prev_register, prev_group_chat = _install(
        db, config,
    )

    class _FakeSourceDescriber:
        async def describe_sources(self, sources: tuple[str, ...]) -> list[str | None]:
            assert sources == ('base64://AQ==',)
            return ['一只猫']

    monkeypatch.setattr(chat, '_image_describer', _FakeSourceDescriber())
    try:
        response = await platform_inbound(_body(
            '月璃看[图片]',
            imageSources=['base64://AQ=='],
        ))
    finally:
        app_state.chat = prev_chat
        app_state.registry = prev_registry
        app_state.register_platform_stream = prev_register
        app_state.group_chat_config = prev_group_chat

    payload = json.loads(response.body)
    buffered = chat._buffers[payload['streamId']]
    assert len(buffered) == 1
    assert buffered[0].image_description_task is not None
    await buffered[0].image_description_task
    stored = [message.content for message in chat.memory.working_memory(payload['streamId'], 20)]
    assert any('[图片：一只猫]' in content for content in stored)


async def test_group_backfill_records_observations_and_deduplicates(
    db: sqlite3.Connection,
) -> None:
    """群历史回填只写观察不触发回复，重复提交不会重复落库。"""
    config = Config()
    config.bot.name = '月璃'
    chat, prev_chat, prev_registry, prev_register, prev_group_chat = _install(db, config)
    body = GroupBackfillBody(
        platform='qq',
        streamExternalId='86420',
        messages=[
            GroupBackfillMessageBody(
                externalMessageId='9001',
                messageSeq=7,
                createdAt=1_750_000_000_000,
                senderExternalId='97531',
                senderNickname='账号昵称',
                senderGroupCard='小李',
                text='停机期间群里说的话',
            ),
        ],
    )
    try:
        first = await platform_group_backfill(body)
        second = await platform_group_backfill(body)
    finally:
        app_state.chat = prev_chat
        app_state.registry = prev_registry
        app_state.register_platform_stream = prev_register
        app_state.group_chat_config = prev_group_chat

    assert first.body.decode('utf-8') == '{"written":1}'
    assert second.body.decode('utf-8') == '{"written":0}'
    stored = [message.content for message in chat.memory.working_memory(2, 20)]
    assert stored == ['停机期间群里说的话']


async def test_group_backfill_seeds_cursor_from_existing_messages(
    db: sqlite3.Connection,
) -> None:
    """旧事件无 externalMessageId 时，用已落库正文与时间窗播种首启游标。"""
    config = Config()
    config.bot.name = '月璃'
    chat, prev_chat, prev_registry, prev_register, prev_group_chat = _install(db, config)
    registry: StreamRegistry = app_state.registry
    context = registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='小李',
        first_seen_at=current_time(),
    )
    old_at = current_time() - 60_000
    chat.memory.append_message(
        context.stream.id,
        context.person.id,
        'user',
        '这条重启前已经落库',
        old_at,
    )
    body = GroupBackfillBody(
        platform='qq',
        streamExternalId='86420',
        messages=[
            GroupBackfillMessageBody(
                externalMessageId='old-9001',
                messageSeq=5,
                createdAt=old_at,
                senderExternalId='97531',
                senderNickname='账号昵称',
                senderGroupCard='小李',
                text='这条重启前已经落库',
            ),
            GroupBackfillMessageBody(
                externalMessageId='new-9002',
                messageSeq=6,
                createdAt=current_time() + 1_000,
                senderExternalId='97531',
                senderNickname='账号昵称',
                senderGroupCard='小李',
                text='停机期间的新消息',
            ),
        ],
    )
    try:
        first = await platform_group_backfill(body)
        second = await platform_group_backfill(body)
    finally:
        app_state.chat = prev_chat
        app_state.registry = prev_registry
        app_state.register_platform_stream = prev_register
        app_state.group_chat_config = prev_group_chat

    assert first.body.decode('utf-8') == '{"written":1}'
    assert second.body.decode('utf-8') == '{"written":0}'
    stored = [message.content for message in chat.memory.working_memory(context.stream.id, 20)]
    assert stored == ['这条重启前已经落库', '停机期间的新消息']
