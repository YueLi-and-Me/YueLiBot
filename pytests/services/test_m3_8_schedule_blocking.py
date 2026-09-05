"""验证日程生成、对话打断和入站超时的协作行为。

本模块覆盖日程任务的生成边界、活动对话对后台任务的打断以及平台入站处理的超时错误，
依赖 DayPlanService 和 ChatService 的异步实现。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncIterator, Dict, List

import asyncio
import json

from src.core.common.clock import now as current_time
from src.core.persona.state import PersonaState
from src.core.schedule.plan import DayPlanService, day_plan_date
from src.core.config.schema import Config
from src.core.services.chat import ChatService, InboundMessage
from src.core.observe import events as trace
from src.core.observe.store import event_store


class _Store:
    def __init__(self) -> None:
        self.values: Dict[str, Any] = {}

    def read_json(self, key: str, fallback: Any) -> Any:
        return self.values.get(key, fallback)

    def write_json(self, key: str, value: Any) -> None:
        self.values[key] = value


class _SlowScheduleGenerator:
    def __init__(self, date: str) -> None:
        self.date = date
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def generate(self, _prompt: str) -> str:
        self.started.set()
        await self.release.wait()
        self.finished.set()
        return json.dumps(_generated_plan(self.date), ensure_ascii=False)


class _FailingScheduleGenerator:
    async def generate(self, _prompt: str) -> str:
        raise RuntimeError('日程模型暂时不可用')


class _RecordingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.messages: List[List[Dict[str, Any]]] = []

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        self.messages.append(messages)
        self.started.set()
        yield {'text': '<say>收到</say>'}


class _SequencedGateProvider:
    def __init__(self) -> None:
        self.releases: List[asyncio.Event] = []
        self.tasks: List[asyncio.Task[Any]] = []

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        del messages
        index = len(self.releases)
        release = asyncio.Event()
        task = asyncio.current_task()
        assert task is not None
        self.releases.append(release)
        self.tasks.append(task)
        await release.wait()
        yield {'text': f'<say>第{index + 1}条回复</say>'}


async def _noop_push(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    return None


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


def _make_schedule(store: _Store, generator: Any, db: Any) -> DayPlanService:
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


async def _finish_turn(chat: ChatService, stream_id: int) -> None:
    await chat._tick()
    inflight = chat._inflight.get(stream_id)
    if inflight is not None:
        await inflight.task


async def _wait_for_provider_calls(provider: _SequencedGateProvider, count: int) -> None:
    for _ in range(100):
        if len(provider.releases) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f'模型请求数未达到 {count}，实际为 {len(provider.releases)}')


async def test_chat_reaches_llm_before_slow_schedule_finishes(db: Any) -> None:
    """日程生成未完成时，实时对话仍要立即进入模型请求。"""
    event_store.clear()
    trace.reset_for_tests()
    now = current_time()
    store = _Store()
    generator = _SlowScheduleGenerator(day_plan_date(now))
    provider = _RecordingProvider()
    schedule = _make_schedule(store, generator, db)
    chat = ChatService(db, provider, provider, provider, _noop_push, cfg=Config())
    chat.set_schedule(schedule)
    context = chat.desktop_context

    send_task = asyncio.create_task(
        chat.send(InboundMessage(text='现在能马上回复吗', context=context))
    )
    await send_task
    await chat._tick()
    request_started_in_time = True
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=0.9)
    except TimeoutError:
        request_started_in_time = False
    finally:
        generator.release.set()
        await send_task
        await _finish_turn(chat, context.stream.id)

    generated_plan = await schedule.ensure(now)
    assert request_started_in_time, 'llm_request 未在 0.9 秒内出现，实时对话仍被日程生成阻塞'
    entries = event_store.since(0).events
    user_input = next(entry for entry in entries if entry['kind'] == 'user_input')
    llm_request = next(entry for entry in entries if entry['kind'] == 'llm_request')
    assert llm_request['at'] - user_input['at'] < 1_000
    assert generator.finished.is_set()
    assert generated_plan.theme == '慢慢整理今天新冒出来的念头。'

    # 方向不冒充当前活动；问起时注入的是时间线的冷启动事实，而不是 intentions。
    await chat.send(InboundMessage(text='你在干嘛呢', context=context))
    await _finish_turn(chat, context.stream.id)
    prompt = json.dumps(provider.messages[-1], ensure_ascii=False)
    assert '刚停下来，还没决定接下来做什么' in prompt
    mentioned = [
        intention.what for intention in generated_plan.intentions
        if intention.what in prompt
    ]
    assert len(mentioned) <= 1


async def test_schedule_failure_is_logged_without_blocking_chat(
    db: Any,
    capsys: Any,
) -> None:
    """后台日程失败要明确记录，实时对话仍然正常完成。"""
    now = current_time()
    provider = _RecordingProvider()
    schedule = _make_schedule(_Store(), _FailingScheduleGenerator(), db)
    chat = ChatService(db, provider, provider, provider, _noop_push, cfg=Config())
    chat.set_schedule(schedule)
    context = chat.desktop_context

    await chat.send(InboundMessage(text='日程失败也要回复', context=context))
    await _finish_turn(chat, context.stream.id)
    await schedule.ensure(now)

    issue = schedule.generation_issue(now)
    output = capsys.readouterr().out
    assert issue is not None and issue.kind == 'provider-error'
    assert '方向生成失败' in output
    assert provider.messages


async def test_second_message_waits_while_first_schedule_preparation_finishes(db: Any) -> None:
    """同一 stream 在飞期间的新消息进入下一批，不取消正在准备的回合。"""
    now = current_time()
    generator = _SlowScheduleGenerator(day_plan_date(now))
    provider = _SequencedGateProvider()
    chat = ChatService(db, provider, provider, provider, _noop_push, cfg=Config())
    chat.set_schedule(_make_schedule(_Store(), generator, db))
    context = chat.desktop_context

    first_send = asyncio.create_task(
        chat.send(InboundMessage(text='第一条消息', context=context))
    )
    await first_send
    await chat._tick()
    await generator.started.wait()
    second_send = asyncio.create_task(
        chat.send(InboundMessage(text='第二条消息', context=context))
    )
    await asyncio.sleep(0)
    generator.release.set()
    await asyncio.gather(first_send, second_send)
    provider.releases[0].set()
    await provider.tasks[0]
    await asyncio.sleep(0)
    await chat._tick()
    await _wait_for_provider_calls(provider, 2)
    provider.releases[1].set()
    await provider.tasks[1]

    assistant_messages = [
        message.content
        for message in chat.memory.working_memory(context.stream.id)
        if message.role == 'assistant'
    ]
    assert assistant_messages == ['<say>第1条回复</say>', '<say>第2条回复</say>']


async def test_back_to_back_messages_share_one_model_request(db: Any) -> None:
    """任务尚未开始时连续消息合并为同一个回复批次。"""
    provider = _RecordingProvider()
    chat = ChatService(db, provider, provider, provider, _noop_push, cfg=Config())
    context = chat.desktop_context

    await chat.send(InboundMessage(text='第一条消息', context=context))
    await chat.send(InboundMessage(text='第二条消息', context=context))
    await chat._tick()
    turn = chat._inflight[context.stream.id]
    await turn.task

    messages = chat.memory.working_memory(context.stream.id)
    assert [message.content for message in messages if message.role == 'user'] == [
        '第一条消息',
        '第二条消息',
    ]
    assert [message.content for message in messages if message.role == 'assistant'] == [
        '<say>收到</say>',
    ]
    assert len(provider.messages) == 1
