"""
Phase 2 tests — persona + schedule.
移植自 src/core/memory/store.test.ts（人格部分）和 src/core/schedule/plan.test.ts。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from yueli.memory.store import MemoryStore, FactInput
from yueli.persona.state import MoodDelta, Persona, describe_persona, PersonaState
from yueli.schedule.plan import (
    DayPlan, DayPlanService, DayPlanSlot, ScheduleSleepState,
    day_plan_date, fallback_day_plan, parse_day_plan,
)

HOUR = 3_600_000
DAY = 24 * HOUR


@pytest.fixture
def db_with_persona():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    MemoryStore(db)   # executes DDL + SEED (creates persona row)
    yield db
    db.close()


@pytest.fixture
def persona(db_with_persona):
    return Persona(db_with_persona)


class TestPersona:
    def test_favor_raises_intimacy_lowers_tsundere(self, persona):
        before = persona.get()
        after = persona.apply_mood(MoodDelta(favor=3))
        assert after.intimacy > before.intimacy
        assert after.tsundere < before.tsundere

    def test_reliance_grows_slower_than_intimacy(self, persona):
        before = persona.get()
        after = persona.apply_mood(MoodDelta(favor=3))
        assert (after.intimacy - before.intimacy) > (after.reliance - before.reliance)

    def test_clamps_extreme_model_values(self, persona):
        s = persona.apply_mood(MoodDelta(favor=999))
        assert s.intimacy <= 12 + 3 * 1.2 + 0.001

    def test_neglect_raises_tsundere_drops_reliance(self, persona):
        before = persona.get()
        after = persona.apply_elapsed(before.updated_at + 7 * DAY, 0)
        assert after.tsundere > before.tsundere
        assert after.reliance < before.reliance
        assert after.intimacy > before.intimacy - 7

    def test_each_turn_advances_persona(self, persona):
        before = persona.get()
        s = before
        for i in range(10):
            s = persona.apply_turn(before.updated_at + i * 60_000)
        assert s.intimacy > before.intimacy + 3
        assert s.tsundere < before.tsundere
        assert s.energy < before.energy

    def test_awake_time_drains_energy(self, persona):
        before = persona.get()
        after = persona.apply_elapsed(before.updated_at + 6 * HOUR, 0)
        assert after.energy < before.energy

    def test_sleep_recovers_energy(self, persona):
        before = persona.get()
        after = persona.apply_elapsed(before.updated_at + 13 * HOUR, 9)
        assert after.energy > before.energy

    def test_describe_persona_no_digits(self):
        text = describe_persona(PersonaState(intimacy=73, tsundere=30, reliance=85, energy=15, updated_at=0))
        assert not any(c.isdigit() for c in text)
        assert '别扭' in text
        assert '依赖' in text
        assert '困' in text

    def test_different_states_different_descriptions(self):
        cold = describe_persona(PersonaState(intimacy=5, tsundere=0, reliance=5, energy=90, updated_at=0))
        close = describe_persona(PersonaState(intimacy=95, tsundere=0, reliance=95, energy=90, updated_at=0))
        assert cold != close
        assert '距离感' in cold
        assert '依恋' in close

    def test_daily_snapshot_dedup(self, persona):
        from datetime import datetime
        now = int(datetime(2051, 7, 15, 9, 0).timestamp() * 1000)
        persona.snapshot_daily(now)
        persona.apply_turn(now + 60_000)
        persona.snapshot_daily(now + 2 * 60_000)
        assert len(persona.snapshots()) == 1


# ─── Schedule tests ────────────────────────────────────────────────────────

class _MockStore:
    def __init__(self):
        self._data: dict = {}
    def read_json(self, key, fallback):
        return self._data.get(key, fallback)
    def write_json(self, key, value):
        self._data[key] = value


def _generated_plan(date: str, theme: str = '今天慢慢整理自己的小想法。') -> DayPlan:
    return DayPlan(
        date=date,
        slots=[
            DayPlanSlot('08:20', '在翻看今天想做的小事', '刚醒不久，声音很轻'),
            DayPlanSlot('09:40', '在给昨天画到一半的图补最后一小块颜色', '有点较真'),
            DayPlanSlot('11:40', '在听一会儿舒缓的歌', '放松又随意'),
            DayPlanSlot('13:20', '在为晚点想说的话反复打腹稿', '心不在焉'),
            DayPlanSlot('15:10', '在整理零散的灵感', '专注但愿意聊天'),
            DayPlanSlot('17:20', '在把刚刚想通的小事记进自己的备忘', '释然'),
            DayPlanSlot('19:30', '在画一点小涂鸦', '心情轻快'),
            DayPlanSlot('22:50', '在慢慢收尾今天的想法', '有一点困倦'),
        ],
        bedtime_hint='23:30', wake_hint='08:00', theme=theme,
        carry_over='把昨天画到一半的小图补完最后一块颜色。',
    )


def _make_service(store, gen_fn) -> DayPlanService:
    from yueli.schedule.plan import _plan_to_dict

    class _Gen:
        async def generate(self, prompt: str) -> str:
            return await gen_fn(prompt)

    return DayPlanService(
        store=store,
        persona_description=lambda: '你很喜欢他，语气自然柔软。',
        interaction_density=lambda _now: '最近几天你们偶尔聊聊。',
        anniversary_at=lambda: 0,
        energy=lambda: 60,
        last_interaction_at=lambda: None,
        generator=_Gen(),
    )


class TestSchedule:
    def test_same_day_generated_once(self):
        from yueli.schedule.plan import _plan_to_dict
        store = _MockStore()
        calls = 0

        async def gen(prompt):
            nonlocal calls
            calls += 1
            return json.dumps(_plan_to_dict(_generated_plan('2032-07-15')))

        svc = _make_service(store, gen)
        now = int(__import__('datetime').datetime(2032, 7, 15, 10, 0).timestamp() * 1000)

        async def run():
            first = await svc.ensure(now)
            second = await svc.ensure(now)
            third = await svc.ensure(now)
            return first, second, third, calls

        first, second, third, c = asyncio.run(run())
        assert c == 1
        assert second.date == first.date
        assert third.theme == first.theme

    def test_generation_failure_returns_fallback(self):
        store = _MockStore()

        async def gen(prompt):
            raise RuntimeError('provider unavailable')

        svc = _make_service(store, gen)
        now = int(__import__('datetime').datetime(2032, 7, 16, 12, 10).timestamp() * 1000)

        async def run():
            plan = await svc.ensure(now)
            desc = svc.describe(now, ScheduleSleepState(asleep=False, drowsy=False))
            return plan, desc

        plan, desc = asyncio.run(run())
        assert len(plan.slots) > 0
        assert '吃午饭' in desc
