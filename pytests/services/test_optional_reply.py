from __future__ import annotations

from json import loads
from inspect import getsource
from typing import Any, AsyncIterator

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.agent.action import TurnAction
from src.core.api.auth import token_manager
from src.core.api.http import PlatformInboundBody, platform_inbound, router
from src.core.api.state import app_state
from src.core.config.schema import Config
from src.core.observe.store import event_store
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import InboundMessage
from src.core.services.chat import ChatService


class _CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, str]]:
        self.calls += 1
        yield {'text': '不应生成'}


class _SilentPolicy:
    @property
    def decision_source(self) -> str:
        return type(self).__name__

    async def decide(self, context: Any) -> Any:
        return TurnAction(action='silent', reason='验收策略保持安静')


async def _push_collector(target: list[tuple[str, Any, int]], channel: str, payload: Any, stream_id: int) -> None:
    target.append((channel, payload, stream_id))


async def _send_and_wait(chat: ChatService, text: str) -> int:
    context = chat.desktop_context
    await chat.send(InboundMessage(text=text, context=context))
    await chat._tick()
    turn = chat._active_turns[context.stream.id]
    await asyncio.wait_for(chat._inflight[context.stream.id].task, timeout=5)
    return turn


async def test_silent_action_keeps_user_message_without_model_or_error(db) -> None:
    provider = _CountingProvider()
    pushed: list[tuple[str, Any, int]] = []
    chat = ChatService(
        db, provider, provider, provider,
        lambda channel, payload, stream_id: _push_collector(pushed, channel, payload, stream_id),
        cfg=Config(),
        action_policy=_SilentPolicy(),
    )
    vector_calls = 0
    reinforcement_calls = 0

    async def count_vector(_query: str) -> None:
        nonlocal vector_calls
        vector_calls += 1

    def count_reinforcement(*_args: Any, **_kwargs: Any) -> None:
        nonlocal reinforcement_calls
        reinforcement_calls += 1

    chat._vector.embed_query = count_vector
    chat.memory.reinforce_recalled_facts = count_reinforcement

    turn = await _send_and_wait(chat, '这条消息只记住，不回复')

    assert provider.calls == 0
    assert vector_calls == 0
    assert reinforcement_calls == 0
    assert not any(channel == 'chat.error' for channel, _, _ in pushed)
    assert chat.memory.working_memory(chat.desktop_context.stream.id)[-1].content == '这条消息只记住，不回复'
    stages = [
        entry['stage']
        for entry in event_store.search(turn_id=turn, kinds=['stage'], limit=100).events
    ]
    assert 'expression' not in stages
    assert 'generating' not in stages
    assert 'failed' not in stages
    stage = event_store.current_stages()[0]
    assert stage['turnId'] == turn
    assert stage['stage'] == 'gated'
    assert '验收策略保持安静' in stage['detail']


async def test_silent_action_event_is_queryable_by_turn(db) -> None:
    provider = _CountingProvider()
    chat = ChatService(
        db, provider, provider, provider,
        lambda channel, payload, stream_id: None,
        cfg=Config(),
        action_policy=_SilentPolicy(),
    )

    turn = await _send_and_wait(chat, '记录动作事件')
    token_manager.configure('optional-reply-token')
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer optional-reply-token'
        response = client.get(f'/events?turnId={turn}&kind=turn_action')
    assert response.status_code == 200
    entries = response.json()['events']
    decision = next(entry for entry in entries if entry['kind'] == 'turn_action')

    assert decision['streamId'] == chat.desktop_context.stream.id
    assert decision['turnId'] == turn
    assert decision['action'] == 'silent'
    assert decision['reason'] == '验收策略保持安静'
    assert decision['decisionSource'] == '_SilentPolicy'


async def test_default_action_event_records_always_reply_policy(db) -> None:
    provider = _CountingProvider()
    chat = ChatService(
        db, provider, provider, provider,
        lambda channel, payload, stream_id: None,
        cfg=Config(),
    )

    turn = await _send_and_wait(chat, '记录默认策略来源')
    decision = event_store.search(turn_id=turn, kinds=['turn_action']).events[0]

    assert decision['decisionSource'] == 'TurnPlanner(AlwaysReplyPolicy)'
    assert '本批输入字符数=8' in decision['reason']
    assert 'inputChars' not in decision


async def test_platform_inbound_accepts_reply_and_silent_without_turn_id(db) -> None:
    previous_chat = app_state.chat
    previous_registry = app_state.registry
    previous_register = app_state.register_platform_stream
    previous_group_chat = app_state.group_chat_config
    registry = StreamRegistry(db)
    provider = _CountingProvider()

    try:
        for index, policy in enumerate((None, _SilentPolicy()), start=1):
            chat = ChatService(
                db, provider, provider, provider,
                lambda channel, payload, stream_id: None,
                cfg=Config(),
                action_policy=policy,
            )
            app_state.chat = chat
            app_state.registry = registry
            app_state.register_platform_stream = None
            app_state.group_chat_config = Config().group_chat
            response = await platform_inbound(PlatformInboundBody(
                platform='qq',
                streamKind='direct',
                streamExternalId=f'private-{index}',
                senderExternalId=f'user-{index}',
                senderNickname=f'用户 {index}',
                senderGroupCard='',
                text=f'动作 {index}',
                mentionedMe=False,
                externalMessageId=str(index),
            ))
            payload = loads(response.body)
            assert 'turnId' not in payload
            assert payload['accepted'] is True
            await chat._tick()
            await chat._inflight[payload['streamId']].task
    finally:
        app_state.chat = previous_chat
        app_state.registry = previous_registry
        app_state.register_platform_stream = previous_register
        app_state.group_chat_config = previous_group_chat

async def test_reply_turn_recalls_facts_only_once(db, monkeypatch) -> None:
    provider = _CountingProvider()
    chat = ChatService(
        db, provider, provider, provider,
        lambda channel, payload, stream_id: None,
        cfg=Config(),
    )
    calls = 0
    original = chat.memory.recall_facts_in_scope

    def count_recall(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(chat.memory, 'recall_facts_in_scope', count_recall)

    await _send_and_wait(chat, '正常回复只召回一次事实')

    assert calls == 1


async def test_resumption_is_consumed_once_without_manual_restore(db) -> None:
    source = getsource(ChatService.send)

    assert 'pending_resumption' not in source
    assert 'session.resumption_gap_ms =' not in source
