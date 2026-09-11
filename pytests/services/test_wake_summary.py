"""起床汇总：深睡区间记录、owner 私聊选择、一次性投递与失败可诊断性。"""

from __future__ import annotations

from typing import Any, List

import asyncio
import sqlite3

import pytest

import src.core.services.chat.service as chat_service_module
from src.core.awareness.sleep import DeepSleepPeriod, SleepState, SleepStateController
from src.core.config.schema import Config
from src.core.platform_io.types import DeliveryReceipt, OutboundMessage
from src.core.schedule.timeline import ActivityTimeline
from src.core.services.chat import ChatService


async def _noop_push(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    return None


class _Broker:
    """记录真实出站消息的最小 PlatformBroker 替身。"""

    def __init__(self) -> None:
        self.sent: List[OutboundMessage] = []

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        await asyncio.sleep(0)
        self.sent.append(message)
        return DeliveryReceipt(message.stream.platform, message.stream.id, [])


class _ScriptedProvider:
    """按脚本输出文本的主动模型替身，同时记录收到的提示词。"""

    def __init__(self, script: List[str] | None = None) -> None:
        self.script = script or []
        self.calls: List[List[dict]] = []

    async def stream(self, messages: List[dict], **_kwargs: Any):
        self.calls.append(messages)
        for text in self.script:
            yield {'text': text}


class _FailingProvider:
    """调用即抛异常的主动模型替身。"""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, messages: List[dict], **_kwargs: Any):
        self.calls += 1
        raise RuntimeError('模型连接失败')
        yield {}


class _RecordingLogger:
    """按事件名记录 info/warning/debug 调用的日志替身。"""

    def __init__(self) -> None:
        self.calls: List[tuple[str, str, dict]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.calls.append(('info', event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self.calls.append(('warning', event, fields))

    def debug(self, event: str, **fields: Any) -> None:
        self.calls.append(('debug', event, fields))


def _make_chat(db: sqlite3.Connection, broker: _Broker | None = None) -> ChatService:
    return ChatService(db, None, None, None, _noop_push, cfg=Config(), broker=broker)


def _owner_context(chat: ChatService):
    owner = chat._registry.owner_person()
    chat._registry.set_sole_identity(owner, 'qq', '24680', '主人')
    context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='24680',
        sender_external_id='24680',
        sender_nickname='主人',
        sender_group_card='',
        first_seen_at=1_700_000_000_000,
    )
    assert context.person.id == owner.id
    return owner, context


def _sleep_state(*periods: DeepSleepPeriod) -> SleepState:
    return SleepState(
        asleep=False,
        just_woke=True,
        resting=False,
        level='awake',
        activity_id=9999,
        deep_sleep_periods=tuple(periods),
    )


def _insert_activity(
    db: sqlite3.Connection,
    *,
    kind: str,
    energy_pace: int,
    started_at: int,
    expected_until: int,
    ended_at: int | None = None,
) -> int:
    cursor = db.execute(
        """INSERT INTO activities
             (kind, doing, mood, energy_pace, mood_pace, advances,
              started_at, expected_until, ended_at, source)
           VALUES (?, ?, ?, ?, 0, NULL, ?, ?, ?, 'decided')""",
        (kind, f'{kind} 活动', '状态平稳', energy_pace, started_at, expected_until, ended_at),
    )
    db.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def test_deep_sleep_period_excludes_time_before_process_observation(
    db: sqlite3.Connection,
) -> None:
    """进程启动时深睡已经开始，汇总窗口只能从第一次观察时刻算起。"""

    sleep_at = 1_700_000_000_000
    observed_at = sleep_at + 30 * 60_000
    wake_at = sleep_at + 2 * 60 * 60_000
    deep_id = _insert_activity(
        db,
        kind='sleep',
        energy_pace=3,
        started_at=sleep_at,
        expected_until=wake_at,
    )
    timeline = ActivityTimeline(db)
    controller = SleepStateController(timeline)

    first = controller.current(observed_at)
    assert first.asleep is True and first.level == 'deep'

    db.execute('UPDATE activities SET ended_at = ? WHERE id = ?', (wake_at, deep_id))
    _insert_activity(
        db,
        kind='awake',
        energy_pace=0,
        started_at=wake_at,
        expected_until=wake_at + 60 * 60_000,
    )
    db.commit()
    state = controller.current(wake_at + 1_000)

    assert state.just_woke is True
    assert [(item.activity_id, item.started_at, item.ended_at) for item in state.deep_sleep_periods] == [
        (deep_id, observed_at, wake_at)
    ]


def test_deep_then_light_sleep_records_only_deep_period(db: sqlite3.Connection) -> None:
    """深睡转浅睡时还不算醒；到真正醒来只汇总深睡那一段。"""

    deep_start = 1_700_000_000_000
    light_start = deep_start + 60 * 60_000
    wake_at = light_start + 60 * 60_000
    deep_id = _insert_activity(
        db,
        kind='sleep',
        energy_pace=3,
        started_at=deep_start,
        expected_until=light_start,
    )
    timeline = ActivityTimeline(db)
    controller = SleepStateController(timeline)
    assert controller.current(deep_start + 1_000).level == 'deep'

    db.execute('UPDATE activities SET ended_at = ? WHERE id = ?', (light_start, deep_id))
    light_id = _insert_activity(
        db,
        kind='sleep',
        energy_pace=2,
        started_at=light_start,
        expected_until=wake_at,
    )
    db.commit()
    light_state = controller.current(light_start + 1_000)
    assert light_state.asleep is True and light_state.level == 'light'
    assert light_state.deep_sleep_periods == ()

    db.execute('UPDATE activities SET ended_at = ? WHERE id = ?', (wake_at, light_id))
    _insert_activity(
        db,
        kind='awake',
        energy_pace=0,
        started_at=wake_at,
        expected_until=wake_at + 60 * 60_000,
    )
    db.commit()
    state = controller.current(wake_at + 1_000)

    assert [item.activity_id for item in state.deep_sleep_periods] == [deep_id]
    assert state.deep_sleep_periods[0].ended_at == light_start


async def test_three_deep_messages_are_summarized_once(db: sqlite3.Connection) -> None:
    """同一段深睡里的三条消息只触发一次汇总投递。"""

    broker = _Broker()
    chat = _make_chat(db, broker)
    owner, context = _owner_context(chat)
    provider = _ScriptedProvider(['<say>刚醒，看到你找我了。</say>'])
    chat._proactive_provider = provider
    base = 1_700_000_000_000
    for offset, text in ((1_000, '睡了吗'), (2_000, '在吗'), (3_000, '晚安')):
        chat.memory.append_message(
            context.stream.id, owner.id, 'user', text, now=base + offset,
        )

    state = _sleep_state(DeepSleepPeriod(7, base, base + 10_000))
    await chat.summarize_deep_sleep(state)
    await chat.summarize_deep_sleep(state)

    assert len(broker.sent) == 1
    assert broker.sent[0].segments == ['刚醒，看到你找我了。']
    assert broker.sent[0].stream.id == context.stream.id
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE role = 'assistant'"
    ).fetchone()[0] == 1
    prompt_text = '\n'.join(
        message['content'] for message in provider.calls[0]
    )
    assert '睡了吗' in prompt_text and '在吗' in prompt_text and '晚安' in prompt_text


async def test_empty_model_result_sends_nothing(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型返回空正文代表选择不回应；零发送，并记 info 级日志。"""

    broker = _Broker()
    chat = _make_chat(db, broker)
    owner, context = _owner_context(chat)
    provider = _ScriptedProvider([''])
    chat._proactive_provider = provider
    chat.memory.append_message(context.stream.id, owner.id, 'user', '在吗', now=1_700_000_001_000)
    recorder = _RecordingLogger()
    monkeypatch.setattr(chat_service_module, 'logger', recorder)

    await chat.summarize_deep_sleep(
        _sleep_state(DeepSleepPeriod(8, 1_700_000_000_000, 1_700_000_010_000)),
    )

    assert broker.sent == []
    assert any(event == '起床汇总模型选择不回应' for _level, event, _fields in recorder.calls)
    assert not any(event == '起床汇总模型调用失败' for _level, event, _fields in recorder.calls)


async def test_model_failure_is_logged_as_failure_not_silence(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型异常必须记 warning，不能写成「她选择不回应」。"""

    broker = _Broker()
    chat = _make_chat(db, broker)
    owner, context = _owner_context(chat)
    provider = _FailingProvider()
    chat._proactive_provider = provider
    chat.memory.append_message(context.stream.id, owner.id, 'user', '在吗', now=1_700_000_001_000)
    recorder = _RecordingLogger()
    monkeypatch.setattr(chat_service_module, 'logger', recorder)

    state = _sleep_state(DeepSleepPeriod(9, 1_700_000_000_000, 1_700_000_010_000))
    await chat.summarize_deep_sleep(state)
    await chat.summarize_deep_sleep(state)

    assert broker.sent == []
    assert provider.calls == 1
    failures = [event for _level, event, _fields in recorder.calls if event == '起床汇总模型调用失败']
    assert failures == ['起床汇总模型调用失败']
    assert not any('未产生' in event or '不回应' in event for _level, event, _fields in recorder.calls)


async def test_group_and_contact_messages_are_not_mixed_in(db: sqlite3.Connection) -> None:
    """群聊与联系人私聊的消息不能进入 owner 起床汇总。"""

    broker = _Broker()
    chat = _make_chat(db, broker)
    owner, owner_context = _owner_context(chat)
    contact_context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='13579',
        sender_external_id='13579',
        sender_nickname='联系人',
        sender_group_card='',
        first_seen_at=1_700_000_000_000,
    )
    group_context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='group-1',
        sender_external_id='24680',
        sender_nickname='主人',
        sender_group_card='',
        first_seen_at=1_700_000_000_000,
    )
    base = 1_700_000_000_000
    chat.memory.append_message(owner_context.stream.id, owner.id, 'user', '主人私聊', now=base + 1_000)
    chat.memory.append_message(
        contact_context.stream.id, contact_context.person.id, 'user', '联系人私聊', now=base + 2_000,
    )
    chat.memory.append_message(group_context.stream.id, owner.id, 'user', '群里的发言', now=base + 3_000)
    provider = _ScriptedProvider(['<say>醒来看到你的消息了。</say>'])
    chat._proactive_provider = provider

    await chat.summarize_deep_sleep(_sleep_state(DeepSleepPeriod(10, base, base + 10_000)))

    assert len(broker.sent) == 1
    assert broker.sent[0].stream.id == owner_context.stream.id
    prompt_text = '\n'.join(message['content'] for message in provider.calls[0])
    assert '主人私聊' in prompt_text
    assert '联系人私聊' not in prompt_text
    assert '群里的发言' not in prompt_text


async def test_latest_owner_direct_stream_wins(db: sqlite3.Connection) -> None:
    """多个 owner 私聊有消息时，只汇总本次最新收到消息的那个会话。"""

    broker = _Broker()
    chat = _make_chat(db, broker)
    owner, first_context = _owner_context(chat)
    chat._registry.set_sole_identity(owner, 'matrix', 'owner-matrix', '主人')
    second_context = chat._registry.resolve_inbound(
        platform='matrix',
        stream_kind='direct',
        stream_external_id='owner-matrix',
        sender_external_id='owner-matrix',
        sender_nickname='主人',
        sender_group_card='',
        first_seen_at=1_700_000_000_000,
    )
    base = 1_700_000_000_000
    chat.memory.append_message(first_context.stream.id, owner.id, 'user', '较早的消息', now=base + 1_000)
    chat.memory.append_message(second_context.stream.id, owner.id, 'user', '最新的消息', now=base + 2_000)
    provider = _ScriptedProvider(['<say>我看到了。</say>'])
    chat._proactive_provider = provider

    await chat.summarize_deep_sleep(_sleep_state(DeepSleepPeriod(11, base, base + 10_000)))

    assert len(broker.sent) == 1
    assert broker.sent[0].stream.id == second_context.stream.id
    prompt_text = '\n'.join(message['content'] for message in provider.calls[0])
    assert '最新的消息' in prompt_text
    assert '较早的消息' not in prompt_text


async def test_new_message_during_generation_discards_stale_summary(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型生成期间来了新消息，旧窗口汇总放弃投递，交给正常回合。"""

    broker = _Broker()
    chat = _make_chat(db, broker)
    owner, context = _owner_context(chat)
    base = 1_700_000_000_000
    chat.memory.append_message(context.stream.id, owner.id, 'user', '第一条', now=base + 1_000)

    class _LateMessageProvider:
        async def stream(self, messages: List[dict], **_kwargs: Any):
            chat.memory.append_message(
                context.stream.id, owner.id, 'user', '生成期间的新消息', now=base + 2_000,
            )
            yield {'text': '<say>刚醒。</say>'}

    chat._proactive_provider = _LateMessageProvider()
    recorder = _RecordingLogger()
    monkeypatch.setattr(chat_service_module, 'logger', recorder)

    await chat.summarize_deep_sleep(_sleep_state(DeepSleepPeriod(12, base, base + 10_000)))

    assert broker.sent == []
    assert any(event == '起床汇总放弃投递' for _level, event, _fields in recorder.calls)


async def test_resleep_during_generation_discards_stale_summary(
    db: sqlite3.Connection,
) -> None:
    """模型生成期间她又睡着，放弃这次旧窗口的投递。"""

    broker = _Broker()
    chat = _make_chat(db, broker)
    owner, context = _owner_context(chat)
    base = 1_700_000_000_000
    chat.memory.append_message(context.stream.id, owner.id, 'user', '第一条', now=base + 1_000)

    asleep = SleepState(asleep=True, just_woke=False, resting=False, level='deep')

    class _ResleepProvider:
        async def stream(self, messages: List[dict], **_kwargs: Any):
            chat._sleep_state = lambda: asleep
            yield {'text': '<say>刚醒。</say>'}

    chat._proactive_provider = _ResleepProvider()

    await chat.summarize_deep_sleep(_sleep_state(DeepSleepPeriod(13, base, base + 10_000)))

    assert broker.sent == []


async def test_busy_stream_does_not_consume_once_per_wakeup(db: sqlite3.Connection) -> None:
    """取得占用权前失败不记账；会话空闲后仍能补上同一次起床汇总。"""

    broker = _Broker()
    chat = _make_chat(db, broker)
    owner, context = _owner_context(chat)
    base = 1_700_000_000_000
    chat.memory.append_message(context.stream.id, owner.id, 'user', '在吗', now=base + 1_000)
    chat._proactive_provider = _ScriptedProvider(['<say>刚醒。</say>'])
    state = _sleep_state(DeepSleepPeriod(14, base, base + 10_000))

    assert chat.claim_stream(context.stream.id, 'reply') is True
    await chat.summarize_deep_sleep(state)
    assert broker.sent == []

    chat.release_stream(context.stream.id, 'reply')
    await chat.summarize_deep_sleep(state)
    assert len(broker.sent) == 1
