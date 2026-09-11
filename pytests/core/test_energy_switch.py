"""精力开关必须冻结所有入口，恢复时只计算关闭时长的基线回归。"""

from math import exp
from typing import Any

import sqlite3

import pytest

from src.core.awareness.interest import factors_for
from src.core.awareness.sleep import SleepStateController
from src.core.config.schema import ScheduleConfig
from src.core.config.settings_webui import load_schema
from src.core.persona.state import ENERGY_BASELINE, ENERGY_TAU, ElapsedEffect, EventDelta, Persona, PersonaState
from src.core.schedule.plan import DayPlanService, ScheduleSleepState
from src.core.schedule.timeline import ActivityTimeline, parse_activity_decision

HOUR_MS = 3_600_000


def test_disabled_energy_freezes_turn_event_and_elapsed(db: sqlite3.Connection) -> None:
    person_id = db.execute("SELECT id FROM persons WHERE kind = 'owner'").fetchone()[0]
    start = 10 * HOUR_MS
    db.execute('UPDATE persona_self SET energy = 20, mood = 20, updated_at = ?', (start,))
    db.commit()
    persona = Persona(db)
    initial = persona.get(person_id)
    persona.set_energy_enabled(False, start)
    persona.apply_turn(person_id, start + 1, weight=1.0)
    persona.apply_event(person_id, EventDelta(favor=1, energy=-3), start + 2, weight=1.0)
    result = persona.apply_elapsed(person_id, start + HOUR_MS, ElapsedEffect(100, 10))
    assert result.energy == 20
    assert result.mood > 20
    assert result.intimacy > initial.intimacy
    assert persona.settled_at() == start + HOUR_MS

    # 重启仍处于关闭态时不能覆盖关闭起点；最后不足一小时也算进回归时长。
    restarted = Persona(db)
    restarted.set_energy_enabled(False, start + HOUR_MS)
    assert db.execute("SELECT value FROM meta WHERE key = 'energy_disabled_at'").fetchone()[0] == str(start)
    end = start + 3 * HOUR_MS // 2
    restarted.set_energy_enabled(True, end)
    expected = ENERGY_BASELINE + (20 - ENERGY_BASELINE) * exp(-1.5 / ENERGY_TAU)
    assert restarted.get(person_id).energy == pytest.approx(expected)
    assert restarted.settled_at() == end
    assert db.execute("SELECT value FROM meta WHERE key = 'energy_disabled_at'").fetchone() is None
    restarted.set_energy_enabled(True, end + HOUR_MS)
    assert restarted.get(person_id).energy == pytest.approx(expected)
    assert restarted.settled_at() == end


@pytest.mark.parametrize('energy', [0, 30, 100])
def test_disabled_energy_factor_is_neutral(energy: float) -> None:
    assert factors_for('idle', 'light', 50, energy, 0, 0, energy_enabled=False).energy == 1.0


class _Store:
    def read_json(self, _key: str, fallback: Any) -> Any:
        return fallback

    def write_json(self, _key: str, _value: Any) -> None:
        pass


def test_disabled_energy_is_absent_from_behavior_and_planning(db: sqlite3.Connection) -> None:
    service = DayPlanService(
        db=db, store=_Store(), persona_state=lambda: PersonaState(50, 0, 50, 0),
        interaction_density=lambda _now: '', anniversary_at=lambda: 0,
        last_interaction_at=lambda: None, generator=None,
        character_name="测试角色", character_personality="按自己的节奏生活",
        schedule_config=ScheduleConfig(energy_enabled=False),
    )
    prompt = service.describe(1_800_000_000_000, ScheduleSleepState(asleep=False))
    context = service.activity_decision_context(1_800_000_000_000)
    assert '精疲力尽' not in prompt
    assert '精力' not in context.persona
    assert not context.energy_enabled
    assert parse_activity_decision(
        '{"decision":"switch","activity":{"kind":"sleep","doing":"睡觉",'
        '"mood":"安静","energyPace":3,"moodPace":0,"minutes":60}}',
        energy_enabled=False, intention_count=0, require_backfill=False,
    ) is None


def test_disabling_energy_ends_existing_sleep_without_stopping_timeline(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO activities (kind, doing, mood, energy_pace, mood_pace, started_at, expected_until, source) "
        "VALUES ('sleep', '睡着', '安静', 3, 0, ?, ?, 'decided')",
        (HOUR_MS, 3 * HOUR_MS),
    )
    db.commit()
    timeline = ActivityTimeline(db)
    state = SleepStateController(timeline, energy_enabled=False).current(2 * HOUR_MS)
    assert not state.asleep
    assert timeline.current(2 * HOUR_MS).kind == 'awake'
    timeline.assert_invariants()


def test_settings_schema_energy_switch_matches_python_model() -> None:
    schema = load_schema()
    section = next(
        section for file in schema['files'] for section in file['sections']
        if section['key'] == 'schedule'
    )
    assert section['label'] == '每日方向与活动'
    assert section['description'] == '控制精力系统的开关、方向生成失败时的备用主题与重试节奏。'
    assert {field['key'] for field in section['fields']} == set(ScheduleConfig.model_fields)
    switch = next(field for field in section['fields'] if field['key'] == 'energy_enabled')
    assert switch['label'] == '启用精力系统'
