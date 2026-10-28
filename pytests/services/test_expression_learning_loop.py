"""后台表达学习循环：一次调用内连续处理多批，某批失败即停。

回填把游标回拨 14 天后积压可达几百条；逐回合只处理一批会让回填拖很久。
这里用 monkeypatch 替换 run_learning 只保留「成功推进游标 / 失败返回 None」的
行为，验证循环的批数与停止条件，不重复覆盖单批内部的语料与校验。
"""

from __future__ import annotations

from typing import Any, List

import pytest

from src.core.agent.expression_learn import (
    LearnReport,
    LearningBatch,
    advance_cursor,
    read_cursor,
)
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


def _seed_messages(chat: ChatService, count: int) -> list[int]:
    """向桌面会话写入 count 条群友消息，返回消息 ID。"""

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


def _report() -> LearnReport:
    """返回一份字段齐全的空对账；循环只看是否为 None。"""

    return LearnReport(
        message_count=0,
        units=0,
        candidates=0,
        learned=0,
        increased=0,
        dropped={},
        eliminated=0,
        remaining=0,
    )


@pytest.mark.asyncio
async def test_one_call_processes_multiple_batches_until_backlog_below_trigger(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """积压够多时一次调用连续处理多批，直到剩余条数不足触发阈值。"""

    import src.core.services.chat.background as chat_module

    calls: List[int] = []

    async def fake_run_learning(
        store: Any,
        _provider: Any,
        _db: Any,
        *,
        stream_id: int,
        batch: LearningBatch,
        **_kwargs: Any,
    ) -> LearnReport:
        calls.append(batch.batch[-1].message_id)
        advance_cursor(store, stream_id, batch.batch[-1].message_id)
        return _report()

    monkeypatch.setattr(chat_module, 'run_learning', fake_run_learning)
    chat = _chat(db)
    message_ids = _seed_messages(chat, 100)
    stream_id = chat.desktop_context.stream.id

    await chat._maybe_learn_expressions(stream_id)

    # 每批 16 条群友消息：处理 5 批后剩 20 条，不足触发阈值 24 即停止，
    # 剩的 20 条等新消息把积压推过阈值后再学。
    assert len(calls) == 5
    assert calls[-1] == message_ids[79]
    assert read_cursor(chat.memory, stream_id) == message_ids[79]


@pytest.mark.asyncio
async def test_one_failed_batch_stops_the_loop(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """某批失败即停：已成功批次的游标保留，失败批次不推进。"""

    import src.core.services.chat.background as chat_module

    calls: List[int] = []

    async def flaky_run_learning(
        store: Any,
        _provider: Any,
        _db: Any,
        *,
        stream_id: int,
        batch: LearningBatch,
        **_kwargs: Any,
    ) -> LearnReport | None:
        calls.append(batch.batch[-1].message_id)
        if len(calls) == 2:
            return None
        advance_cursor(store, stream_id, batch.batch[-1].message_id)
        return _report()

    monkeypatch.setattr(chat_module, 'run_learning', flaky_run_learning)
    chat = _chat(db)
    message_ids = _seed_messages(chat, 100)
    stream_id = chat.desktop_context.stream.id

    await chat._maybe_learn_expressions(stream_id)

    assert len(calls) == 2
    # 第一批成功推进到第 16 条；第二批失败不推进，等后续重试或达到跳批上限。
    assert read_cursor(chat.memory, stream_id) == message_ids[15]
