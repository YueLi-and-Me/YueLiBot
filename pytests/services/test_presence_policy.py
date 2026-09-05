"""第 4b 步存在感策略验收。"""

from __future__ import annotations

from typing import Any, AsyncIterator

from src.core.agent.action import ActionContext, TurnPlanner
from src.core.config.schema import Config
from src.core.memory.store import MemoryStore
from src.core.observe.store import event_store
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import InboundMessage
from src.core.services.chat import ChatService


class _ReplyProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        self.calls += 1
        yield {'text': '<say emotion="normal">收到</say>'}


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


def _presence_policy(memory: MemoryStore, *, draw: float):
    from src.core.agent.action import PresenceActionPolicy

    return PresenceActionPolicy(
        base_probability=0.8,
        decay_strength=3.0,
        window_minutes=10,
        assistant_reply_count_since=memory.assistant_reply_count_since,
        message_count_since=memory.message_count_since,
        probability_draw=lambda: draw,
        clock=lambda: 1_000_000,
    )


async def test_low_presence_keeps_base_probability(db) -> None:
    memory = MemoryStore(db)
    policy = _presence_policy(memory, draw=0.79)

    action = await policy.decide(ActionContext(
        turn_id=1,
        stream_id=1,
        messages=(),
        batch_text='',
    ))

    assert action.should_reply is True
    assert '占比=0.0000' in action.reason
    assert '实际概率=0.8000' in action.reason


async def test_high_presence_silences_without_models_and_records_reason(db) -> None:
    config = Config()
    registry = StreamRegistry(db)
    context = registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='group-high-presence',
        sender_external_id='member-high-presence',
        sender_nickname='群成员',
        sender_group_card='群成员',
        first_seen_at=1_000_000,
    )
    provider = _ReplyProvider()
    chat = ChatService(
        db, provider, provider, provider, _noop,
        cfg=config,
    )
    now = 1_000_000
    for index, role in enumerate(('assistant', 'assistant', 'assistant', 'user')):
        chat.memory.append_message(
            context.stream.id,
            context.person.id if role == 'user' else None,
            role,
            role,
            now - index,
        )
    chat.set_action_policy(
        'group',
        TurnPlanner(_presence_policy(chat.memory, draw=0.4)),
    )

    await chat.send(InboundMessage(text='月璃，在吗', context=context))
    await chat._tick()
    turn = chat._active_turns[context.stream.id]
    await chat._inflight[context.stream.id].task
    action_event = next(
        event for event in event_store.search(turn_id=turn, kinds=['turn_action']).events
        if event['decisionSource'] == 'TurnPlanner(PresenceActionPolicy)'
    )
    stage = next(
        event for event in event_store.search(turn_id=turn, kinds=['stage']).events
        if event['stage'] == 'gated'
    )
    observation = next(
        event for event in event_store.search(turn_id=turn, kinds=['observation']).events
    )
    stored_messages = chat.memory.working_memory(context.stream.id, 20)

    assert provider.calls == 0
    assert action_event['action'] == 'silent'
    assert '占比=0.6000' in action_event['reason']
    assert '实际概率=0.2857' in action_event['reason']
    assert '占比=0.6000' in stage['detail']
    assert '占比=0.6000' in observation['reason']
    assert [message.content for message in stored_messages].count('月璃，在吗') == 1


async def test_mandatory_at_mention_bypasses_high_presence(db) -> None:
    from src.core.agent.action import PresenceActionPolicy

    config = Config()
    config.group_chat.at_mention_must_reply = True
    registry = StreamRegistry(db)
    context = registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='group-presence',
        sender_external_id='member-presence',
        sender_nickname='群成员',
        sender_group_card='群成员',
        first_seen_at=1_000_000,
    )
    provider = _ReplyProvider()
    chat = ChatService(
        db, provider, provider, provider, _noop,
        cfg=config,
        action_policies={
            'group': TurnPlanner(PresenceActionPolicy(
                base_probability=0.0,
                decay_strength=3.0,
                window_minutes=10,
                assistant_reply_count_since=lambda _stream_id, _since: 99,
                message_count_since=lambda _stream_id, _since: 100,
                probability_draw=lambda: 0.99,
                clock=lambda: 1_000_000,
            )),
        },
    )

    await chat.send(InboundMessage(
        text='@月璃 必须回复',
        context=context,
        mentioned_me=True,
    ))
    await chat._tick()
    turn = chat._active_turns[context.stream.id]
    await chat._inflight[context.stream.id].task

    assert turn > 0
    assert provider.calls == 1
    assert isinstance(chat._default_action_policy, TurnPlanner)
