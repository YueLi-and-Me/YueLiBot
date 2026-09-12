"""全局时间结算必须由无头心跳推进，并与所有人物的回合共用游标。"""

from datetime import datetime
from math import exp
from typing import Tuple

import asyncio
import sqlite3

import pytest

from src.core.config.schema import Config
from src.core.persona.state import ENERGY_BASELINE, ENERGY_RATES, ENERGY_TAU, MOOD_TAU, TURN_ENERGY_COST
from src.core.platform_io.registry import StreamRegistry
from src.core.schedule.plan import DayPlanService
from src.core.schedule.timeline import ActivityTimeline
from src.core.services.chat import ChatService
from src.core.services.proactive import AwarenessService


HOUR = 3_600_000
START = int(datetime(2032, 7, 15, 23).timestamp() * 1000)


async def _push(*_args) -> None:
    pass


def _services(db: sqlite3.Connection) -> Tuple[ChatService, AwarenessService]:
    """使用真实人格、日程和活动存储，模型不装配，桌面主动发言关闭。"""
    cfg = Config()
    chat = ChatService(db, None, None, None, _push, cfg=cfg)
    timeline = ActivityTimeline(db)
    schedule = DayPlanService(
        chat.memory, lambda: chat.persona.get(chat.desktop_context.person.id),
        lambda _now: '', lambda: 0, lambda: None, '测试角色', '安静',
        timeline=timeline,
    )
    chat.set_schedule(schedule)
    service = AwarenessService(chat, schedule, timeline, cfg, _push)
    db.execute('UPDATE persona_self SET energy = 39, mood = 40, updated_at = ?', (START,))
    db.execute('UPDATE persona_bond SET intimacy = 60, updated_at = ?', (START,))
    db.execute('DELETE FROM persona_snapshots')
    db.commit()
    return chat, service


def _activity(db: sqlite3.Connection, kind: str, pace: int, end: int) -> None:
    db.execute(
        '''INSERT INTO activities
           (kind, doing, mood, energy_pace, mood_pace, started_at, expected_until, source)
           VALUES (?, '测试活动', '平静', ?, 1, ?, ?, 'decided')''',
        (kind, pace, START, end),
    )
    db.commit()


def _energy(before: float, rate: float, hours: float = 1) -> float:
    return ENERGY_BASELINE + (before + rate * hours - ENERGY_BASELINE) * exp(-hours / ENERGY_TAU)


@pytest.mark.parametrize('kind,pace', [('awake', -1), ('rest', 2), ('sleep', 3)])
async def test_headless_minute_ticks_settle_without_messages(db, monkeypatch, kind, pace):
    chat, service = _services(db)
    _activity(db, kind, pace, START + 3 * HOUR)
    assert not service._enabled
    initial_messages = db.execute('SELECT count(*) FROM messages').fetchone()[0]
    rate = ENERGY_RATES[(kind, pace)]
    for minute in range(1, 121):
        now = START + minute * 60_000
        monkeypatch.setattr('src.core.services.proactive.current_time', lambda: now)
        await service._tick()
        await asyncio.sleep(0)
        if minute < 60:
            assert chat.persona.settled_at() == START
            assert chat.persona.get(1).energy == 39
            assert db.execute('SELECT count(*) FROM persona_snapshots').fetchone()[0] == 0
    state = chat.persona.get(1)
    assert state.energy == pytest.approx(_energy(_energy(39, rate), rate))
    assert state.intimacy == pytest.approx(59.95)
    assert chat.persona.settled_at() == START + 2 * HOUR
    assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == initial_messages
    snapshot = db.execute('SELECT energy, mood, captured_at FROM persona_snapshots').fetchone()
    assert tuple(snapshot) == (state.energy, state.mood, START + 2 * HOUR)
    # 同一时刻的回合与心跳均不得重复积分或重复衰减。
    changes = db.total_changes
    chat.settle_elapsed(chat.desktop_context, now)
    assert db.total_changes == changes


def test_contact_turn_settles_global_time_but_only_owner_bond_decays(db):
    chat, _service = _services(db)
    _activity(db, 'rest', 2, START + 3 * HOUR)
    context = StreamRegistry(db).resolve_inbound(
        'onebot11', 'group', 'test-group', 'test-contact', '测试成员', '', START,
    )
    contact = context.person
    assert context.stream.kind == 'group'
    assert not context.relationship_signals_enabled
    before = chat.persona.get(contact.id)
    chat.settle_elapsed(context, START + HOUR)
    state = chat.persona.get(contact.id)
    assert state.energy == pytest.approx(_energy(39, ENERGY_RATES[('rest', 2)]))
    assert state.intimacy == before.intimacy
    assert state.updated_at == before.updated_at
    assert chat.persona.get(1).intimacy == pytest.approx(59.975)
    chat.persona.apply_turn(contact.id, START + HOUR, weight=0.2)
    assert chat.persona.get(contact.id).energy == pytest.approx(state.energy - TURN_ENERGY_COST * 0.2)
    assert chat.persona.settled_at() == START + HOUR


def test_service_clips_offline_gap_and_credits_backfill(db):
    chat, _service = _services(db)
    _activity(db, 'awake', 0, START + HOUR)
    chat.settle_time(START + 3 * HOUR)
    first = _energy(39, ENERGY_RATES[('awake', 0)])
    assert chat.persona.get(1).energy == pytest.approx(first)
    assert chat.persona.settled_at() == START + HOUR
    db.execute('UPDATE activities SET ended_at = ?', (START + HOUR,))
    db.execute(
        '''INSERT INTO activities
           (kind, doing, mood, energy_pace, mood_pace, started_at, expected_until, source)
           VALUES ('sleep', '睡觉', '平静', 3, 0, ?, ?, 'backfilled')''',
        (START + HOUR, START + 3 * HOUR),
    )
    db.commit()
    chat.settle_time(START + 3 * HOUR)
    assert chat.persona.get(1).energy == pytest.approx(_energy(first, ENERGY_RATES[('sleep', 3)], 2))
    assert chat.persona.settled_at() == START + 3 * HOUR


@pytest.mark.parametrize('desktop_enabled', [False, True])
async def test_activity_read_observes_newly_settled_energy(db, monkeypatch, desktop_enabled):
    chat, service = _services(db)
    service._enabled = desktop_enabled
    _activity(db, 'rest', 2, START + HOUR)
    now = START + HOUR
    monkeypatch.setattr('src.core.services.proactive.current_time', lambda: now)
    original = service._timeline.current
    observed = []

    def current(at):
        observed.append(chat.persona.get(1).energy)
        return original(at)

    monkeypatch.setattr(service._timeline, 'current', current)
    await service._tick()
    await asyncio.sleep(0)
    assert observed
    assert all(value == pytest.approx(_energy(39, ENERGY_RATES[('rest', 2)])) for value in observed)


def test_disabled_energy_still_settles_mood_and_owner_intimacy(db):
    chat, _service = _services(db)
    _activity(db, 'awake', -1, START + HOUR)
    chat.persona.set_energy_enabled(False, START)
    chat.settle_time(START + HOUR)
    state = chat.persona.get(1)
    assert state.energy == 39
    assert state.mood == pytest.approx(50 + (40 + 2 - 50) * exp(-1 / MOOD_TAU))
    assert state.intimacy == pytest.approx(59.975)
    assert chat.persona.settled_at() == START + HOUR
