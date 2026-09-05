"""
AwarenessService 编排测试。

只验证编排是否正确调用 classify、budget 和 sleep 纯函数，不重复验证这些函数本身。
测试将 compose_proactive 和 speak 替换为记录调用的 stub，不访问真实模型服务。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import asyncio

import src.core.services.proactive as proactive_module
from src.core.awareness.sleep import SleepState
from src.desktop.classify import ForegroundInfo, classify
from src.core.awareness.intent import IntentType, PendingIntent
from src.core.awareness.interest import FULL, InterestState
from src.core.agent.parser import PromiseEvent
from src.core.config.schema import Config
from src.core.observe.store import event_store
from src.core.persona.state import PersonaState
from src.core.schedule.plan import DayPlanService
from src.core.schedule.timeline import ActivityTimeline
from src.core.services.chat import ChatService
from src.core.services.proactive import AwarenessService
from src.desktop.sensor import DesktopSensor


async def _noop_push(channel, payload, stream_id=1):
    return None


def _make_chat(db) -> ChatService:
    return ChatService(
        db=db,
        chat_provider=None,
        proactive_provider=None,
        summary_provider=None,
        push_event=_noop_push,
        cfg=Config(),
    )


def _make_ready_chat(db) -> ChatService:
    """chat.ready=True，但 compose_proactive/speak 是记录调用的 stub，不打真实 LLM。"""
    chat = _make_chat(db)
    chat._chat_provider = object()
    chat.compose_proactive = AsyncMock(return_value=[{'text': '在干嘛呢'}])
    chat.speak_claimed = MagicMock(return_value=1)
    return chat


class _CaptureProactiveProvider:
    """记录主动请求，并返回一条可解析的固定回复。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def stream(self, messages, **_kwargs):
        self.messages = messages
        yield {'text': '<say>我先说句话</say>'}


async def test_proactive_request_keeps_non_system_content_for_gemini(db) -> None:
    """Gemini 兼容网关抽离 system 后仍必须有非空 contents。"""
    provider = _CaptureProactiveProvider()
    chat = _make_chat(db)
    chat._proactive_provider = provider
    situation = '编辑器里正在处理一个报错'

    result = await chat.compose_proactive(chat.desktop_context, situation)

    assert result == [{'text': '我先说句话'}]
    assert [message['role'] for message in provider.messages] == ['system', 'user']
    assert situation in provider.messages[0]['content']
    assert situation not in provider.messages[1]['content']
    assert provider.messages[1]['content'].strip()


def _make_service(
    chat,
    schedule=None,
    push_event=_noop_push,
    timeline=None,
) -> AwarenessService:
    cfg = Config()
    # 主动搭话的出口只有桌面 stream；桌宠开关默认关闭，测试按真实桌面部署显式打开。
    cfg.desktop_pet.enabled = True
    sensor = DesktopSensor(cfg, push_event)
    return AwarenessService(
        chat=chat,
        schedule=schedule,
        timeline=timeline or ActivityTimeline(chat.memory._db),
        cfg=cfg,
        push_event=push_event,
        sensor=sensor,
    )


async def test_on_foreground_updates_classification(db):
    """window_changed() 那几条断言随后台截图链路一起删了——它只有那个端点在读。
    窗口切换本身仍然驱动主动搭话，走的是 monitor 的 obs.window_changed。"""
    svc = _make_service(_make_chat(db))

    svc.on_foreground({'process': 'Code.exe', 'title': 'a.py', 'fullscreen': False})
    await asyncio.sleep(0.02)
    assert svc._sensor._last_classified.activity == 'coding'
    assert svc.current_app() == 'VS Code'

    svc.on_foreground({'process': 'chrome.exe', 'title': '某网页', 'fullscreen': False})
    await asyncio.sleep(0.02)
    assert svc._sensor._last_classified.activity == 'browsing'


async def test_meeting_process_does_not_trigger_speak(db):
    chat = _make_ready_chat(db)
    svc = _make_service(chat)

    svc.on_foreground({'process': 'Zoom.exe', 'title': 'meeting', 'fullscreen': False})
    await asyncio.sleep(0.05)

    chat.compose_proactive.assert_not_awaited()
    chat.speak_claimed.assert_not_called()


async def test_window_change_triggers_scene_speak_when_allowed(db):
    chat = _make_ready_chat(db)
    svc = _make_service(chat)

    svc.on_foreground({'process': 'Code.exe', 'title': 'main.py', 'fullscreen': False})
    await asyncio.sleep(0.05)

    chat.compose_proactive.assert_awaited_once()
    chat.speak_claimed.assert_called_once()
    assert svc._budget.used == 1
    stages = [
        entry['stage']
        for entry in event_store.since(0).events
        if entry['kind'] == 'stage'
    ]
    assert stages[-3:] == ['generating', 'dispatching', 'replied']


async def test_scene_intents_are_not_held_by_a_fixed_cooldown(db):
    chat = _make_ready_chat(db)
    svc = _make_service(chat)

    svc.on_foreground({'process': 'Code.exe', 'title': 'a', 'fullscreen': False})
    await asyncio.sleep(0.05)
    assert chat.compose_proactive.await_count == 1

    # 立刻切到另一个窗口：固定冷却已删除，仍由每日预算与情境预留控制。
    svc.on_foreground({'process': 'chrome.exe', 'title': 'b', 'fullscreen': False})
    await asyncio.sleep(0.05)
    assert chat.compose_proactive.await_count == 2


async def test_sleep_state_pushed_only_on_transition(db):
    chat = _make_chat(db)

    calls: list[tuple[str, dict]] = []

    async def fake_push(channel, payload):
        calls.append((channel, payload))

    svc = _make_service(chat, push_event=fake_push)

    svc._sleep.current = MagicMock(side_effect=[
        SleepState(asleep=False, just_woke=False, resting=False),
        SleepState(asleep=False, just_woke=False, resting=False),
        SleepState(asleep=True, just_woke=False, resting=False),
    ])

    await svc._refresh_activity_state(1_000)
    await svc._refresh_activity_state(2_000)
    await svc._refresh_activity_state(3_000)

    assert len(calls) == 2   # 初次上报 + 一次真实翻转；中间那次状态没变，不重复推
    assert calls[0][1]['asleep'] is False
    assert calls[1][1]['asleep'] is True


def test_sleep_state_comes_from_current_activity(db):
    """睡眠状态只来自当前 sleep 活动，不再反查前一天的计划时刻。"""
    chat = _make_chat(db)
    now = int(datetime(2026, 8, 8, 0, 3).timestamp() * 1000)
    db.execute(
        '''INSERT INTO activities
             (kind, doing, mood, energy_pace, mood_pace, advances,
              started_at, expected_until, ended_at, source)
           VALUES ('sleep', '睡觉', '睡着后不会回应', 3, 0, NULL, ?, ?, NULL, 'decided')''',
        (now - 60_000, now + 60_000),
    )
    db.commit()
    svc = _make_service(chat)
    state = svc._sleep.current(now)

    assert state.asleep is True


async def test_startup_wires_providers_and_shutdown_stops_poll_task(db):
    chat = _make_chat(db)
    svc = _make_service(chat)

    await svc.startup()
    await asyncio.sleep(0.05)
    assert chat._activity is not None
    assert chat._sleep_state is not None
    assert svc._poll_task is not None and not svc._poll_task.done()

    await svc.shutdown()
    assert svc._poll_task is None


async def test_blocked_interest_stashes_intent_and_spends_the_impulse(db):
    chat = _make_ready_chat(db)
    svc = _make_service(chat)
    classified = classify(ForegroundInfo('Code.exe'))
    now = proactive_module.current_time()
    svc._budget.used = 5
    svc._interest = InterestState(value=FULL, updated_at=now)

    await svc._consider_speak(classified, now, IntentType.Idle)

    assert [intent.intent_type for intent in svc._pending] == [IntentType.Idle]
    assert svc._interest.value == 0


async def test_pending_promise_waits_until_earliest_and_wins_priority(db):
    chat = _make_ready_chat(db)
    svc = _make_service(chat)
    svc._sensor._last_classified = classify(ForegroundInfo('Code.exe'))
    now = 1_760_000_000_000
    promise = PendingIntent(IntentType.Promise, now + 3_600_000, now + 10_800_000, '', False, '周六一起打游戏吧')
    idle = PendingIntent(IntentType.Idle, now, now + 60_000, 'coding', False)
    svc._pending = [idle, promise]

    await svc._flush_pending(now)
    assert chat.compose_proactive.await_count == 1
    assert '写代码' in chat.compose_proactive.await_args.args[1]
    assert len(svc._pending) == 1 and svc._pending[0] is promise

    await svc._flush_pending(now + 3_600_000)
    assert chat.compose_proactive.await_count == 2
    assert '周六一起打游戏吧' in chat.compose_proactive.await_args.args[1]


async def test_expired_scene_without_vision_is_dropped(db):
    chat = _make_ready_chat(db)
    svc = _make_service(chat)
    svc._sensor._last_classified = classify(ForegroundInfo('Code.exe'))
    now = 1_760_000_000_000
    svc._pending = [PendingIntent(IntentType.Scene, now - 60_000, now - 1, 'coding', False)]

    await svc._flush_pending(now)

    chat.compose_proactive.assert_not_awaited()
    assert svc._pending == []


async def test_foreground_event_flushes_due_intent_without_waiting_for_tick(db):
    chat = _make_ready_chat(db)
    svc = _make_service(chat)
    info = ForegroundInfo('Code.exe', 'main.py')
    svc._sensor._monitor.observe(info)
    svc._sensor._last_classified = classify(info)
    now = proactive_module.current_time()
    svc._pending = [PendingIntent(IntentType.Promise, now, now + 60_000, '', False, '晚点一起打游戏')]

    svc.on_foreground({'process': 'Code.exe', 'title': 'main.py', 'fullscreen': False})
    await asyncio.sleep(0.05)

    chat.compose_proactive.assert_awaited_once()


async def test_empty_vision_cache_requests_capture_before_speaking(db):
    chat = _make_ready_chat(db)
    calls: list[tuple[str, dict]] = []

    async def fake_push(channel, payload):
        calls.append((channel, payload))

    svc = _make_service(chat, push_event=fake_push)
    svc._sensor._vision = MagicMock()
    svc._sensor._vision.chat_glance.return_value = None
    await svc._consider_speak(classify(ForegroundInfo('Code.exe')), 1_760_000_000_000, IntentType.Scene)

    assert calls == [('vision.capture_request', {'reason': 'scene'})]
    chat.compose_proactive.assert_not_awaited()
    assert svc._pending[0].wants_vision is True


async def test_cached_vision_is_used_without_a_second_capture_request(db):
    chat = _make_ready_chat(db)
    calls: list[tuple[str, dict]] = []

    async def fake_push(channel, payload):
        calls.append((channel, payload))

    svc = _make_service(chat, push_event=fake_push)
    svc._sensor._vision = MagicMock()
    svc._sensor._vision.chat_glance.return_value = '编辑器里正在改一个 Python 文件'
    await svc._consider_speak(classify(ForegroundInfo('Code.exe')), 1_760_000_000_000, IntentType.Scene)

    assert calls == []
    assert '编辑器里正在改一个 Python 文件' in chat.compose_proactive.await_args.args[1]


async def test_expired_vision_intent_speaks_with_honest_no_screen_context(db):
    chat = _make_ready_chat(db)
    svc = _make_service(chat)
    svc._sensor._vision = MagicMock()
    svc._sensor._vision.chat_glance.return_value = None
    svc._sensor._last_classified = classify(ForegroundInfo('Code.exe'))
    now = 1_760_000_000_000
    svc._pending = [PendingIntent(IntentType.Scene, now - 60_000, now - 1, 'coding', True)]

    await svc._flush_pending(now)

    chat.compose_proactive.assert_awaited_once()
    assert '看不到他的屏幕' in chat.compose_proactive.await_args.args[1]


def test_observability_replaces_budget_with_impulse_and_pending(db):
    svc = _make_service(_make_chat(db))
    now = proactive_module.current_time()
    svc._pending = [PendingIntent(IntentType.Promise, now, now + 60_000, '', False, '晚点一起打游戏')]

    fields = svc.observability_fields(now)

    assert 'budget' not in fields
    assert fields['impulse']['interest'] == 0
    assert fields['sensing']['pending'] == [{'type': 'Promise', 'remainingSeconds': 60}]


def test_promise_subject_is_the_users_original_words(db):
    chat = _make_chat(db)
    received: list[tuple[int, str]] = []
    chat.set_promise_handler(lambda at, subject: received.append((at, subject)))
    source = '那我们周六一起打游戏吧，晚上八点可以吗？'

    chat._handle_side_effects(
        chat.desktop_context,
        PromiseEvent(at=1_760_000_000_000, what='打游戏'),
        1_759_000_000_000,
        1,
        source_text=source,
    )

    assert received == [(1_760_000_000_000, source)]


def test_promise_survives_awareness_service_recreation(db):
    chat = _make_chat(db)
    first = _make_service(chat)
    first.stash_promise(1_760_000_000_000, '周六一起打游戏吧')

    restored = _make_service(chat)

    assert len(restored._pending) == 1
    assert restored._pending[0].intent_type == IntentType.Promise
    assert restored._pending[0].subject == '周六一起打游戏吧'


async def test_activity_change_stashes_one_plan_intent(db, monkeypatch):
    """真实活动段变化只登记一次 Plan 意图，不再监听计划时段。"""

    chat = _make_ready_chat(db)
    timeline = ActivityTimeline(db)
    svc = _make_service(chat, timeline=timeline)
    svc._sensor._last_classified = classify(ForegroundInfo('Code.exe'))
    first_at = int(datetime(2026, 8, 4, 9, 30).timestamp() * 1000)
    second_at = int(datetime(2026, 8, 4, 10, 30).timestamp() * 1000)
    db.execute(
        '''INSERT INTO activities
             (kind, doing, mood, energy_pace, mood_pace, advances,
              started_at, expected_until, ended_at, source)
           VALUES ('awake', '看书', '安静', 0, 0, NULL, ?, ?, NULL, 'decided')''',
        (first_at, second_at + 60_000),
    )
    db.commit()
    times = iter([first_at, second_at])
    monkeypatch.setattr(proactive_module, 'current_time', lambda: next(times))

    await svc._tick()
    db.execute('UPDATE activities SET ended_at = ? WHERE ended_at IS NULL', (second_at,))
    db.execute(
        '''INSERT INTO activities
             (kind, doing, mood, energy_pace, mood_pace, advances,
              started_at, expected_until, ended_at, source)
           VALUES ('awake', '做饭', '有点饿', -1, 0, NULL, ?, ?, NULL, 'decided')''',
        (second_at, second_at + 60_000),
    )
    db.commit()
    await svc._tick()

    assert [intent.intent_type for intent in svc._pending] == [IntentType.Plan]

