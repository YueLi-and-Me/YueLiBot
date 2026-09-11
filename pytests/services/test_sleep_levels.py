"""三档门控、固定深睡提示及跨活动段的一次性投递。"""

from typing import Any, List

import asyncio
import sqlite3

import pytest

from src.core.agent.conversation_gate import GateRequest, decide_disposition
from src.core.awareness.sleep import SleepLevel, SleepStateController
from src.core.config.schema import Config
from src.core.platform_io.types import DeliveryReceipt, InboundMessage, OutboundMessage, StreamKind
from src.core.runtime.clock import now as current_time
from src.core.schedule.timeline import ActivityTimeline
from src.core.services.chat import ChatService
from src.core.services.chat.outbound import DEEP_SLEEP_NOTICE


@pytest.mark.parametrize('level', ['deep', 'light', 'drowsy'])
@pytest.mark.parametrize('kind,mentioned', [('direct', False), ('group', True), ('group', False), ('desktop', False)])
def test_sleep_response_matrix(level: SleepLevel, kind: StreamKind, mentioned: bool) -> None:
    result = decide_disposition(GateRequest(
        stream_kind=kind, mentioned_me=mentioned, name_mentioned=False,
        sleep_level=level, at_mention_must_reply=True,
        replies_in_window=0, max_replies_in_window=3,
    ))
    if level == 'deep':
        assert (result.disposition, result.reason_codes) == ('drop', ('deep_sleep',))
    elif kind in ('desktop', 'direct'):
        assert (result.disposition, result.reason_codes) == ('force', ('direct_conversation',))
    elif mentioned:
        assert (result.disposition, result.reason_codes) == ('force', ('at_mention_must_reply',))
    else:
        reason = 'light_sleep' if level == 'light' else 'attention_filtered'
        assert (result.disposition, result.reason_codes) == ('drop', (reason,))


def test_light_sleep_real_mention_without_must_reply_is_deliberate() -> None:
    result = decide_disposition(GateRequest(
        stream_kind='group', mentioned_me=True, name_mentioned=False,
        sleep_level='light', at_mention_must_reply=False,
        replies_in_window=9, max_replies_in_window=3,
    ))
    assert result.disposition == 'deliberate'
    assert result.reason_codes == ('direct_mention',)


@pytest.mark.parametrize('self_message,pokes,reason', [(True, 0, 'self_message'), (False, 99, 'poke_repeat')])
def test_self_and_repeat_poke_precede_deep_sleep(self_message: bool, pokes: int, reason: str) -> None:
    result = decide_disposition(GateRequest(
        stream_kind='direct', mentioned_me=True, name_mentioned=True, sleep_level='deep',
        at_mention_must_reply=True, replies_in_window=0, max_replies_in_window=3,
        is_self_message=self_message, poked_me=pokes > 0, pokes_in_window=pokes,
    ))
    assert result.reason_codes == (reason,)


def insert_sleep(db: sqlite3.Connection, now: int, pace: int = 3) -> int:
    db.execute('UPDATE activities SET ended_at = ? WHERE ended_at IS NULL', (now,))
    cursor = db.execute(
        "INSERT INTO activities (kind, doing, mood, energy_pace, mood_pace, started_at, expected_until, source) "
        "VALUES ('sleep', '睡觉', '安静', ?, 0, ?, ?, 'decided')", (pace, now, now + 3_600_000),
    )
    db.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


class Broker:
    def __init__(self) -> None:
        self.sent: List[OutboundMessage] = []
        self.fail = False

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError('测试投递失败')
        self.sent.append(message)
        return DeliveryReceipt(message.stream.platform, message.stream.id, [])


async def noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    pass


@pytest.mark.parametrize('kind', ['direct', 'desktop'])
async def test_deep_sleep_never_calls_model_and_sends_once_per_segment(db: sqlite3.Connection, kind: str) -> None:
    broker = Broker()
    events = []

    async def emit(channel: str, payload: Any, _stream_id: int) -> None:
        events.append((channel, payload))

    # 无模型也必须可以发送 zzz；若落进旧的 provider 检查会产出 chat.error。
    chat = ChatService(db, None, None, None, emit, cfg=Config(), broker=broker)
    context = chat.desktop_context if kind == 'desktop' else chat._registry.resolve_inbound(
        platform='qq', stream_kind='direct', stream_external_id='24680',
        sender_external_id='24680', sender_nickname='联系人', sender_group_card='',
        first_seen_at=current_time(),
    )
    controller = SleepStateController(ActivityTimeline(db))
    chat.set_sleep_state_provider(controller.current)
    first = insert_sleep(db, current_time())
    assert chat.current_sleep().level == 'deep'
    assert chat.current_sleep().asleep
    for text in ('第一条', '第二条', '第三条'):
        await chat.send(InboundMessage(text=text, context=context))
        batch = chat._buffers.pop(context.stream.id)
        await chat._start_turn(batch)
    assert chat._deep_sleep_notice_activity_id == first
    assert db.execute("SELECT COUNT(*) FROM messages WHERE role = 'assistant'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM messages WHERE role = 'user'").fetchone()[0] == 3
    assert all(channel != 'chat.error' for channel, _ in events)
    second = insert_sleep(db, current_time() + 1)
    await asyncio.gather(*(chat.send_deep_sleep_notice(context) for _ in range(3)))
    assert chat._deep_sleep_notice_activity_id == second
    assert db.execute("SELECT COUNT(*) FROM messages WHERE role = 'assistant'").fetchone()[0] == 2
    if kind == 'direct':
        assert [message.segments for message in broker.sent] == [[DEEP_SLEEP_NOTICE], [DEEP_SLEEP_NOTICE]]


async def test_failed_notice_is_not_recorded_as_delivered(db: sqlite3.Connection) -> None:
    broker = Broker()
    chat = ChatService(db, None, None, None, noop, cfg=Config(), broker=broker)
    context = chat._registry.resolve_inbound(
        platform='qq', stream_kind='direct', stream_external_id='24680',
        sender_external_id='24680', sender_nickname='联系人', sender_group_card='',
        first_seen_at=current_time(),
    )
    chat.set_sleep_state_provider(SleepStateController(ActivityTimeline(db)).current)
    activity_id = insert_sleep(db, current_time())
    broker.fail = True
    with pytest.raises(RuntimeError, match='测试投递失败'):
        await chat.send_deep_sleep_notice(context)
    assert chat._deep_sleep_notice_activity_id is None
    broker.fail = False
    await chat.send_deep_sleep_notice(context)
    assert chat._deep_sleep_notice_activity_id == activity_id
