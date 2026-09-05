"""后台游标型任务对格式失败的重试与跳批回归。"""

from __future__ import annotations

from typing import Any

import pytest

from src.core.agent.expression_learn import read_cursor as read_expression_cursor
from src.core.agent.fact_extract import read_cursor as read_fact_cursor
from src.core.config.schema import Config
from src.core.services.chat import ChatService


def _chat(db: Any) -> ChatService:
    """构造只启用后台记忆任务的聊天服务。"""
    return ChatService(
        db,
        None,
        None,
        None,
        lambda *_: None,
        cfg=Config(),
        memory_provider=object(),  # type: ignore[arg-type]
    )


def _seed_messages(chat: ChatService, count: int = 40) -> list[int]:
    """向桌面会话写入足以触发两个后台任务的用户消息。"""
    context = chat.desktop_context
    return [
        chat.memory.append_message(
            context.stream.id,
            context.person.id,
            'user',
            f'测试消息 {index}',
            now=index + 1,
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_fact_format_failures_advance_after_retry_limit(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """事实抽取返回 None 也应计为失败，第三次后推进游标。"""
    import src.core.services.chat as chat_module

    async def fail_format(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(chat_module, 'run_extraction', fail_format)
    chat = _chat(db)
    chat._extraction_participants = lambda _batch: [object()]  # type: ignore[method-assign,return-value]
    message_ids = _seed_messages(chat)
    stream_id = chat.desktop_context.stream.id

    await chat._maybe_extract_facts(stream_id, 'desktop')
    await chat._maybe_extract_facts(stream_id, 'desktop')
    assert read_fact_cursor(chat.memory, stream_id) == 0

    await chat._maybe_extract_facts(stream_id, 'desktop')
    assert read_fact_cursor(chat.memory, stream_id) == message_ids[11]


@pytest.mark.asyncio
async def test_expression_format_failures_advance_after_retry_limit(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """表达学习返回 None 也应计为失败，第三次后推进游标。"""
    import src.core.services.chat as chat_module

    async def fail_format(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(chat_module, 'run_learning', fail_format)
    chat = _chat(db)
    message_ids = _seed_messages(chat)
    stream_id = chat.desktop_context.stream.id

    await chat._maybe_learn_expressions(stream_id)
    await chat._maybe_learn_expressions(stream_id)
    assert read_expression_cursor(chat.memory, stream_id) == 0

    await chat._maybe_learn_expressions(stream_id)
    assert read_expression_cursor(chat.memory, stream_id) == message_ids[15]
