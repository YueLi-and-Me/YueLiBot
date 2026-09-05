"""验证 ChatService 按 stream 隔离会话状态。

本模块覆盖不同会话的历史、回复任务和完成状态不会相互串扰，
依赖 ChatService、MemoryStore、StreamRegistry 及测试用流式模型替身。
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, AsyncIterator

import pytest

from src.core.agent.parser import PromiseEvent, SayEndEvent, SayEvent, TextEvent
from src.core.memory.store import MemoryStore
from src.core.platform_io.registry import ConversationContext, StreamRegistry
from src.core.config.schema import Config
from src.core.services.chat import ChatService, InboundMessage


class _GateProvider:
    def __init__(self) -> None:
        self.release: list[asyncio.Event] = []

    async def stream(
        self,
        messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        release = asyncio.Event()
        self.release.append(release)
        await release.wait()
        yield {'text': '<say>收到</say>'}


class _RecordingProvider:
    def __init__(self) -> None:
        self.messages: list[list[dict[str, Any]]] = []

    async def stream(
        self,
        messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        self.messages.append(messages)
        yield {'text': '<say>收到</say>'}


async def _noop(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    return None


async def _wait_for_calls(provider: _GateProvider, count: int) -> None:
    while len(provider.release) < count:
        await asyncio.sleep(0)


@pytest.fixture
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    MemoryStore(connection)
    yield connection
    connection.close()


def _group_context(db: sqlite3.Connection) -> ConversationContext:
    registry = StreamRegistry(db)
    return registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='group-7',
        sender_external_id='contact-42',
        sender_nickname='账号昵称',
        sender_group_card='小李',
        first_seen_at=1_700_000_000_000,
    )


async def test_interrupt_only_cancels_its_own_stream(db: sqlite3.Connection) -> None:
    provider = _GateProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    desktop = chat.desktop_context
    group = _group_context(db)

    await chat.send(InboundMessage(text='桌面消息', context=desktop))
    await chat._tick()
    desktop_turn = chat._inflight[desktop.stream.id]
    await chat.send(InboundMessage(text='群聊消息', context=group))
    await chat._tick()
    group_turn = chat._inflight[group.stream.id]
    await _wait_for_calls(provider, 2)

    chat.interrupt(group.stream.id)

    assert not desktop_turn.cancel_event.is_set()
    assert group_turn.cancel_event.is_set()
    assert desktop.stream.id in chat._inflight
    assert group.stream.id not in chat._inflight

    provider.release[0].set()
    provider.release[1].set()
    await desktop_turn.task
    await group_turn.task
    await asyncio.sleep(0)
    assert desktop.stream.id not in chat._inflight
    assert [message.content for message in chat.memory.working_memory(desktop.stream.id)] == [
        '桌面消息',
        '<say>收到</say>',
    ]
    assert [message.content for message in chat.memory.working_memory(group.stream.id)] == ['群聊消息']


async def test_new_message_waits_until_old_task_callback_releases_stream(db: sqlite3.Connection) -> None:
    provider = _GateProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    context = chat.desktop_context

    await chat.send(InboundMessage(text='第一句', context=context))
    await chat._tick()
    first = chat._inflight[context.stream.id]
    await _wait_for_calls(provider, 1)
    await chat.send(InboundMessage(text='第二句', context=context))
    assert not first.cancel_event.is_set()
    assert chat._inflight[context.stream.id] is first
    provider.release[0].set()
    await first.task
    await asyncio.sleep(0)
    await chat._tick()
    second = chat._inflight[context.stream.id]
    await _wait_for_calls(provider, 2)

    provider.release[1].set()
    await second.task
    await asyncio.sleep(0)
    assert context.stream.id not in chat._inflight


async def test_group_history_adds_display_name_only_when_read(db: sqlite3.Connection) -> None:
    provider = _RecordingProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    context = _group_context(db)
    chat.memory.append_message(context.stream.id, context.person.id, 'user', '今天吃什么', 1_700_000_000_000)

    await chat.send(InboundMessage(text='晚饭呢', context=context))
    await chat._tick()
    inflight = chat._inflight[context.stream.id]
    await inflight.task

    history = provider.messages[-1][1:]
    # 合并后的连续用户消息逐行带发言时刻与群名片；时刻随运行时钟变化，只断言行尾。
    assert history[0]['role'] == 'user'
    lines = history[0]['content'].split('\n')
    assert len(lines) == 2
    assert lines[0].endswith('小李: 今天吃什么')
    assert lines[1].endswith('小李: 晚饭呢')
    assert chat.memory.working_memory(context.stream.id)[0].content == '今天吃什么'


def test_non_desktop_promise_is_not_sent_to_desktop_handler(db: sqlite3.Connection) -> None:
    chat = ChatService(db, None, None, None, _noop, cfg=Config())
    received: list[tuple[int, str]] = []
    chat.set_promise_handler(lambda at, subject: received.append((at, subject)))

    chat._handle_side_effects(
        _group_context(db),
        PromiseEvent(at=1_760_000_000_000, what='一起看电影'),
        1_759_000_000_000,
        1,
        source_text='下次一起看电影吧',
    )

    assert received == []


def test_speech_buffers_stay_on_the_desktop_branch(db: sqlite3.Connection) -> None:
    chat = ChatService(db, None, None, None, _noop, cfg=Config())
    desktop = chat.desktop_context
    group = _group_context(db)
    spoken: list[tuple[str, int]] = []
    chat._speak_audio = lambda text, turn: spoken.append((text, turn))

    chat._track_speech(desktop, SayEvent(), 1)
    chat._track_speech(desktop, TextEvent(value='桌面'), 1)
    chat._track_speech(group, SayEvent(), 2)
    chat._track_speech(group, TextEvent(value='群聊'), 2)
    chat._track_speech(group, SayEndEvent(), 2)
    chat._track_speech(desktop, SayEndEvent(), 1)

    # 群聊文本的出口是平台 driver；绝不能让桌面 TTS 播放它。
    assert spoken == [('桌面', 1)]
