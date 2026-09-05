"""验证对话准备阶段失败时的可观察性。

本模块覆盖日程读取、上下文准备和回复前错误处理，确保失败会写入可追踪记录，
并向调用方返回明确的错误结果而不是静默结束。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Tuple

import asyncio
import json

from src.core.persona.state import PersonaState
from src.core.schedule.plan import DayPlanService, _plan_to_dict, day_plan_date, fallback_day_plan
from src.core.config.schema import Config
from src.core.services.chat import ChatService, InboundMessage


class _Provider:
    async def stream(
        self,
        messages: List[Dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        del messages
        yield {'text': '<say>收到</say>'}


class _ScheduleStore:
    def __init__(self) -> None:
        self.values: Dict[str, Any] = {}

    def read_json(self, key: str, fallback: Any) -> Any:
        return self.values.get(key, fallback)

    def write_json(self, key: str, value: Any) -> None:
        self.values[key] = value


class _FailThenSucceedScheduleGenerator:
    def __init__(self, date: str) -> None:
        self.date = date
        self.calls = 0

    async def generate(self, _prompt: str) -> str:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError('日程模型暂时不可用')
        return json.dumps(_generated_plan(self.date), ensure_ascii=False)


class _InvalidScheduleGenerator:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, _prompt: str) -> str:
        self.calls += 1
        return '{}'


class _SuccessfulScheduleGenerator:
    def __init__(self, date: str) -> None:
        self.date = date
        self.calls = 0

    async def generate(self, _prompt: str) -> str:
        self.calls += 1
        return json.dumps(_generated_plan(self.date), ensure_ascii=False)


def _raise_during_prepare(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError('磁盘满了，写不进去')


async def _run_failed_turn(
    db: Any,
    monkeypatch: Any,
) -> Tuple[ChatService, int, List[Tuple[int, str, Dict[str, Any]]]]:
    events: List[Tuple[int, str, Dict[str, Any]]] = []

    async def push_event(
        channel: str,
        payload: Dict[str, Any],
        stream_id: int,
    ) -> None:
        events.append((stream_id, channel, payload))

    provider = _Provider()
    chat = ChatService(db, provider, provider, provider, push_event, cfg=Config())
    context = chat.desktop_context
    await chat.send(InboundMessage(text='这条消息会写库失败', context=context))
    # 用户消息已在 send() 确认接收时落库，改为模拟随后的上下文准备失败。
    monkeypatch.setattr(chat, '_prepare_turn_context', _raise_during_prepare)
    await chat._tick()
    turn = chat._active_turns[context.stream.id]
    inflight = chat._inflight[context.stream.id]
    await asyncio.gather(inflight.task, return_exceptions=True)
    await asyncio.sleep(0)
    return chat, turn, events


def _make_schedule(store: _ScheduleStore, generator: Any, db: Any) -> DayPlanService:
    return DayPlanService(
        store=store,
        persona_state=lambda: PersonaState(
            intimacy=50.0,
            energy=60.0,
            mood=50.0,
            updated_at=0,
        ),
        interaction_density=lambda _now: '最近偶尔聊聊。',
        anniversary_at=lambda: 0,
        last_interaction_at=lambda: None,
        character_name='测试角色',
        character_personality='测试人设',
        generator=generator,
        db=db,
    )


def _generated_plan(date: str) -> Dict[str, Any]:
    return {
        'date': date,
        'intentions': [
            {'what': '给昨天的画补上颜色', 'carriedDays': 1},
            {'what': '整理零散的灵感', 'carriedDays': 0},
            {'what': '画一点新的小涂鸦', 'carriedDays': 0},
        ],
        'theme': '慢慢整理今天新冒出来的念头。',
        'roughRhythm': '今天顺着实际状态安排节奏',
    }


async def test_prepare_failure_emits_one_chat_error(
    db: Any,
    monkeypatch: Any,
) -> None:
    """准备阶段失败时，原 stream 必须收到且只收到一条错误事件。"""
    chat, turn, events = await _run_failed_turn(db, monkeypatch)
    stream_id = chat.desktop_context.stream.id
    errors = [event for event in events if event[1] == 'chat.error']

    assert errors == [(
        stream_id,
        'chat.error',
        {
            'turnId': turn,
            'kind': 'error',
            'message': '磁盘满了，写不进去',
        },
    )]
    assert stream_id not in chat._inflight


async def test_prepare_failure_has_owned_error_log(
    db: Any,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    """准备阶段异常由对话服务记录，不依赖 asyncio 的未取回异常警告。"""
    await _run_failed_turn(db, monkeypatch)
    output = capsys.readouterr().out

    assert output.count('对话处理失败') == 1
    assert '磁盘满了，写不进去' in output
    assert 'Task exception was never retrieved' not in output


async def test_normal_prepare_does_not_emit_chat_error(db: Any) -> None:
    """准备阶段正常时不额外推送错误，完成后仍清理 inflight。"""
    events: List[Tuple[int, str, Dict[str, Any]]] = []

    async def push_event(
        channel: str,
        payload: Dict[str, Any],
        stream_id: int,
    ) -> None:
        events.append((stream_id, channel, payload))

    provider = _Provider()
    chat = ChatService(db, provider, provider, provider, push_event, cfg=Config())
    context = chat.desktop_context

    await chat.send(InboundMessage(text='正常消息', context=context))
    await chat._tick()
    inflight = chat._inflight[context.stream.id]
    await inflight.task
    await asyncio.sleep(0)

    assert not any(channel == 'chat.error' for _, channel, _ in events)
    assert context.stream.id not in chat._inflight


async def test_provider_failure_does_not_persist_fallback(db: Any) -> None:
    """provider 失败后只返回运行时 fallback，不把它伪装成真实计划落库。"""
    now = int(datetime(2026, 8, 8, 12, 0).timestamp() * 1000)
    store = _ScheduleStore()
    generator = _FailThenSucceedScheduleGenerator(day_plan_date(now))
    schedule = _make_schedule(store, generator, db)

    plan = await schedule.ensure(now)

    assert plan.theme == fallback_day_plan(day_plan_date(now)).theme
    assert store.values == {}
    issue = schedule.generation_issue(now)
    assert issue is not None and issue.kind == 'provider-error'


async def test_invalid_output_does_not_persist_fallback(db: Any) -> None:
    """连续两次结构校验失败也不能把 fallback 写入当天计划。"""
    now = int(datetime(2026, 8, 8, 12, 0).timestamp() * 1000)
    store = _ScheduleStore()
    generator = _InvalidScheduleGenerator()
    schedule = _make_schedule(store, generator, db)

    plan = await schedule.ensure(now)

    assert plan.theme == fallback_day_plan(day_plan_date(now)).theme
    assert generator.calls == 2
    assert store.values == {}
    issue = schedule.generation_issue(now)
    assert issue is not None and issue.kind == 'invalid-output'


async def test_failed_generation_retries_after_ten_minute_cooldown(db: Any) -> None:
    """冷却期内不重复请求，满十分钟后的下一轮后台生成真实计划。"""
    now = int(datetime(2026, 8, 8, 12, 0).timestamp() * 1000)
    retry_interval_ms = 10 * 60_000
    store = _ScheduleStore()
    generator = _FailThenSucceedScheduleGenerator(day_plan_date(now))
    schedule = _make_schedule(store, generator, db)

    await schedule.ensure(now)
    schedule.ensure_background(now + retry_interval_ms - 1)
    await asyncio.sleep(0)

    assert generator.calls == 1
    assert store.values == {}

    schedule.ensure_background(now + retry_interval_ms)
    plan = await schedule.ensure(now + retry_interval_ms)

    assert generator.calls == 2
    assert plan.theme == '慢慢整理今天新冒出来的念头。'
    assert store.values
    assert schedule.generation_issue(now) is None


async def test_legacy_persisted_fallback_is_replaced_by_real_plan(db: Any) -> None:
    """落库的中性方向不能继续阻止当天生成真实方向。"""
    now = int(datetime(2026, 8, 8, 12, 0).timestamp() * 1000)
    date = day_plan_date(now)
    store = _ScheduleStore()
    store.values[f'day_plan:{date}'] = _plan_to_dict(fallback_day_plan(date))
    generator = _SuccessfulScheduleGenerator(date)
    schedule = _make_schedule(store, generator, db)

    plan = await schedule.ensure(now)

    assert generator.calls == 1
    assert plan.theme == '慢慢整理今天新冒出来的念头。'
    assert store.values[f'day_plan:{date}']['theme'] == plan.theme
