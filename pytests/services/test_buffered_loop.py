"""验证聊天缓冲循环与主动发言竞争边界。"""

from __future__ import annotations

from typing import Any, AsyncIterator

import asyncio

from src.core.config.schema import Config
from src.core.memory.store import FactInput
from src.core.observe.store import event_store
from src.core.platform_io.types import InboundMessage
from src.core.services.chat import ChatService


class _BlockingProvider:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        self.calls.append(messages)
        self.started.set()
        await self.release.wait()
        yield {'text': '<say>收到</say>'}


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(
        self,
        messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        self.calls.append(messages)
        yield {'text': '<say>收到</say>'}


class _AdjacentGroupProvider:
    """记录相邻群聊批次，并让第二轮保持在飞以插入后续消息。"""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.second_started = asyncio.Event()
        self.release_second = asyncio.Event()

    async def stream(
        self,
        messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        self.calls.append(messages)
        if len(self.calls) == 1:
            yield {'text': '<say>刚刚回复 Alice 的正文</say>'}
            return
        self.second_started.set()
        await self.release_second.wait()
        yield {'text': '<say>回复 Bob</say>'}


async def _noop(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    return None


async def test_three_messages_share_one_buffered_turn(db) -> None:
    provider = _RecordingProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    context = chat.desktop_context

    for text in ('第一句', '第二句', '第三句'):
        await chat.send(InboundMessage(text=text, context=context))

    assert context.stream.id not in chat._inflight
    await chat._tick()
    inflight = chat._inflight[context.stream.id]
    await inflight.task

    assert len(provider.calls) == 1
    history = '\n'.join(message['content'] for message in provider.calls[0])
    assert all(text in history for text in ('第一句', '第二句', '第三句'))


async def test_group_buffer_starts_with_one_persons_contiguous_prefix(db) -> None:
    provider = _RecordingProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    alice = chat._registry.resolve_inbound(
        'qq', 'group', 'group-mixed', 'alice', 'Alice', 'Alice', 1_800_000_000_000,
    )
    bob = chat._registry.resolve_inbound(
        'qq', 'group', 'group-mixed', 'bob', 'Bob', 'Bob', 1_800_000_000_001,
    )
    chat.memory.add_fact(alice.person.id, FactInput(kind='偏好', content='Alice只喜欢蓝莓'), 1_800_000_000_002)
    chat.memory.add_fact(bob.person.id, FactInput(kind='偏好', content='Bob只喜欢芒果'), 1_800_000_000_003)
    alice_before = chat.persona.get(alice.person.id)
    bob_before = chat.persona.get(bob.person.id)

    await chat.send(InboundMessage(text='Alice第一句', context=alice))
    await chat.send(InboundMessage(text='Bob第一句', context=bob))
    await chat._tick()
    await chat._inflight[alice.stream.id].task

    first_prompt = '\n'.join(message['content'] for message in provider.calls[0])
    assert 'Alice第一句' in first_prompt
    assert 'Bob第一句' not in first_prompt
    assert 'Alice只喜欢蓝莓' in first_prompt
    assert 'Bob只喜欢芒果' not in first_prompt
    assert chat.persona.get(alice.person.id).intimacy > alice_before.intimacy
    assert chat.persona.get(bob.person.id).intimacy == bob_before.intimacy
    assert [message.text for message in chat._buffers[alice.stream.id]] == ['Bob第一句']


async def test_adjacent_group_batch_keeps_previous_assistant_reply_in_history(db) -> None:
    provider = _AdjacentGroupProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    alice = chat._registry.resolve_inbound(
        'qq', 'group', 'group-adjacent', 'alice', 'Alice', 'Alice', 1_800_000_000_000,
    )
    bob = chat._registry.resolve_inbound(
        'qq', 'group', 'group-adjacent', 'bob', 'Bob', 'Bob', 1_800_000_000_001,
    )
    charlie = chat._registry.resolve_inbound(
        'qq', 'group', 'group-adjacent', 'charlie', 'Charlie', 'Charlie', 1_800_000_000_002,
    )

    await chat.send(InboundMessage(text='Alice先说', context=alice))
    await chat.send(InboundMessage(text='Bob随后说', context=bob))
    await chat._tick()
    await chat._inflight[alice.stream.id].task
    await chat._tick()
    await provider.second_started.wait()
    await chat.send(InboundMessage(text='Charlie在第二轮开始后才说', context=charlie))

    second_history = '\n'.join(message['content'] for message in provider.calls[1])
    assert '刚刚回复 Alice 的正文' in second_history
    assert 'Charlie在第二轮开始后才说' not in second_history
    provider.release_second.set()
    await chat._inflight[alice.stream.id].task


async def test_message_arriving_after_tick_before_history_query_waits_for_own_turn(db) -> None:
    provider = _RecordingProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    alice = chat._registry.resolve_inbound(
        'qq', 'group', 'group-window', 'alice', 'Alice', 'Alice', 1_800_000_000_000,
    )
    bob = chat._registry.resolve_inbound(
        'qq', 'group', 'group-window', 'bob', 'Bob', 'Bob', 1_800_000_000_001,
    )

    await chat.send(InboundMessage(text='Alice先进入批次', context=alice))
    await chat._tick()
    # _tick() 已取走 Alice 并创建回合 task，但当前协程尚未让出执行权，历史还未读取。
    await chat.send(InboundMessage(text='Bob在历史查询前到达', context=bob))
    await chat._inflight[alice.stream.id].task

    first_history = '\n'.join(message['content'] for message in provider.calls[0])
    assert 'Alice先进入批次' in first_history
    assert 'Bob在历史查询前到达' not in first_history

    await chat._tick()
    await chat._inflight[alice.stream.id].task
    second_history = '\n'.join(message['content'] for message in provider.calls[1])
    assert 'Bob在历史查询前到达' in second_history


async def test_buffered_message_is_persisted_before_tick_and_survives_shutdown(db) -> None:
    provider = _RecordingProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    context = chat.desktop_context

    await chat.send(InboundMessage(text='已经确认接收', context=context))

    assert [item.content for item in chat.memory.working_memory(context.stream.id)] == ['已经确认接收']
    await chat.shutdown()
    assert [item.content for item in chat.memory.working_memory(context.stream.id)] == ['已经确认接收']


async def test_message_sent_during_inflight_is_persisted_immediately(db) -> None:
    provider = _BlockingProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    context = chat.desktop_context

    await chat.send(InboundMessage(text='在飞消息', context=context))
    await chat._tick()
    await provider.started.wait()
    await chat.send(InboundMessage(text='等待下一轮但已经落库', context=context))

    history = [item.content for item in chat.memory.working_memory(context.stream.id)]
    assert history[-1] == '等待下一轮但已经落库'
    provider.release.set()
    await chat._inflight[context.stream.id].task


async def test_message_during_inflight_waits_for_next_turn(db) -> None:
    provider = _BlockingProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    context = chat.desktop_context

    await chat.send(InboundMessage(text='第一批', context=context))
    if hasattr(chat, '_tick'):
        await chat._tick()
    first = chat._inflight[context.stream.id]
    await provider.started.wait()

    await chat.send(InboundMessage(text='第二批', context=context))

    assert not first.cancel_event.is_set()
    assert [message.text for message in chat._buffers[context.stream.id]] == ['第二批']
    provider.release.set()
    await first.task
    await chat._tick()
    await chat._inflight[context.stream.id].task
    assert len(provider.calls) == 2


async def test_proactive_turn_skips_busy_reply_and_records_competition(db) -> None:
    provider = _BlockingProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    context = chat.desktop_context

    await chat.send(InboundMessage(text='正在回复', context=context))
    if hasattr(chat, '_tick'):
        await chat._tick()
    reply = chat._inflight[context.stream.id]
    await provider.started.wait()

    proactive_turn = chat.speak(context, [{'text': '主动消息'}])

    assert proactive_turn is None
    assert not reply.cancel_event.is_set()
    events = event_store.search(kinds=['turn_competition']).events
    assert any(
        event['streamId'] == context.stream.id
        and event['activeSource'] == 'reply'
        and event['blockedSource'] == 'proactive'
        for event in events
    )
    provider.release.set()
    await reply.task


async def test_interrupt_stops_inflight_without_clearing_buffer(db) -> None:
    provider = _BlockingProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    context = chat.desktop_context

    await chat.send(InboundMessage(text='在飞消息', context=context))
    await chat._tick()
    inflight = chat._inflight[context.stream.id]
    await provider.started.wait()
    await chat.send(InboundMessage(text='缓冲消息', context=context))

    chat.interrupt(context.stream.id)

    assert inflight.cancel_event.is_set()
    assert [message.text for message in chat._buffers[context.stream.id]] == ['缓冲消息']
    provider.release.set()
    await inflight.task
