"""验证人格状态和日程计划的基础行为。

本模块覆盖人格增量、睡眠相关参数和日程计划生成，直接调用当前 Python 实现，
不依赖外部服务或其他项目的测试代码。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from src.core.memory.store import MemoryStore, FactInput
from src.core.persona.state import (
    ElapsedEffect,
    EventDelta,
    Persona,
    PersonaState,
    describe_persona,
)
from src.core.schedule.plan import (
    DayPlan, DayPlanIntention, DayPlanService, ScheduleSleepState,
    day_plan_date, fallback_day_plan, parse_day_plan,
)

HOUR = 3_600_000
DAY = 24 * HOUR
OWNER_PERSON_ID = 1


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
    def test_favor_raises_relationship_depth(self, persona):
        before = persona.get(OWNER_PERSON_ID)
        after = persona.apply_event(OWNER_PERSON_ID, EventDelta(favor=3), weight=1.0)
        assert after.intimacy > before.intimacy

    def test_clamps_extreme_model_values(self, persona):
        s = persona.apply_event(OWNER_PERSON_ID, EventDelta(favor=999), weight=1.0)
        assert s.intimacy <= 12 + 3 * 1.2 + 0.001

    def test_elapsed_time_slowly_lowers_favor(self, persona):
        before = persona.get(OWNER_PERSON_ID)
        after = persona.apply_elapsed(
            OWNER_PERSON_ID,
            before.updated_at + 7 * DAY,
            ElapsedEffect(energy_delta=0.0, mood_delta=0.0),
        )
        assert before.intimacy - 7 < after.intimacy < before.intimacy

    def test_each_turn_advances_persona(self, persona):
        before = persona.get(OWNER_PERSON_ID)
        s = before
        for i in range(10):
            s = persona.apply_turn(OWNER_PERSON_ID, before.updated_at + i * 60_000, weight=1.0)
        assert s.intimacy > before.intimacy + 3
        assert s.energy < before.energy

    def test_awake_time_drains_energy(self, persona):
        before = persona.get(OWNER_PERSON_ID)
        after = persona.apply_elapsed(
            OWNER_PERSON_ID,
            before.updated_at + 6 * HOUR,
            ElapsedEffect(energy_delta=-12.0, mood_delta=0.0),
        )
        assert after.energy < before.energy

    def test_sleep_recovers_energy(self, persona):
        before = persona.get(OWNER_PERSON_ID)
        after = persona.apply_elapsed(
            OWNER_PERSON_ID,
            before.updated_at + 13 * HOUR,
            ElapsedEffect(energy_delta=28.0, mood_delta=0.0),
        )
        assert after.energy > before.energy

    def test_describe_persona_no_digits(self):
        text = describe_persona(
            PersonaState(intimacy=73, energy=15, mood=50.0, updated_at=0)
        )
        assert not any(c.isdigit() for c in text)
        assert '重要' in text
        assert '精力' not in text
        assert '困' not in text

    def test_different_states_different_descriptions(self):
        cold = describe_persona(
            PersonaState(intimacy=5, energy=90, mood=50.0, updated_at=0)
        )
        close = describe_persona(
            PersonaState(intimacy=95, energy=90, mood=50.0, updated_at=0)
        )
        assert cold != close
        assert '关系深度：初识' in cold
        assert '关系深度：深厚' in close

    def test_daily_snapshot_dedup(self, persona):
        from datetime import datetime
        now = int(datetime(2051, 7, 15, 9, 0).timestamp() * 1000)
        persona.snapshot_daily(OWNER_PERSON_ID, now)
        persona.apply_turn(OWNER_PERSON_ID, now + 60_000, weight=1.0)
        persona.snapshot_daily(OWNER_PERSON_ID, now + 2 * 60_000)
        assert len(persona.snapshots(OWNER_PERSON_ID)) == 1


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
        intentions=[
            DayPlanIntention('给昨天画到一半的图补最后一小块颜色', 1),
            DayPlanIntention('整理零散的灵感'),
            DayPlanIntention('把刚刚想通的小事记进备忘'),
        ],
        theme=theme,
        rough_rhythm='上午慢慢进入状态，晚上自然收尾',
    )


def _make_service(store, gen_fn, db) -> DayPlanService:
    from src.core.schedule.plan import _plan_to_dict

    class _Gen:
        async def generate(self, prompt: str) -> str:
            return await gen_fn(prompt)

    return DayPlanService(
        store=store,
        persona_state=lambda: PersonaState(
            intimacy=73.0,
            energy=60.0,
            mood=50.0,
            updated_at=0,
        ),
        interaction_density=lambda _now: '最近几天你们偶尔聊聊。',
        anniversary_at=lambda: 0,
        last_interaction_at=lambda: None,
        character_name='测试角色',
        character_personality='测试人设',
        generator=_Gen(),
        db=db,
    )


class TestSchedule:
    def test_same_day_generated_once(self, db_with_persona):
        from src.core.schedule.plan import _plan_to_dict
        store = _MockStore()
        calls = 0

        async def gen(prompt):
            nonlocal calls
            calls += 1
            return json.dumps(_plan_to_dict(_generated_plan('2032-07-15')))

        svc = _make_service(store, gen, db_with_persona)
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

    def test_generation_failure_returns_fallback(self, db_with_persona):
        store = _MockStore()

        async def gen(prompt):
            raise RuntimeError('provider unavailable')

        svc = _make_service(store, gen, db_with_persona)
        now = int(__import__('datetime').datetime(2032, 7, 16, 12, 10).timestamp() * 1000)

        async def run():
            plan = await svc.ensure(now)
            desc = svc.describe(now, ScheduleSleepState(asleep=False))
            return plan, desc

        plan, desc = asyncio.run(run())
        assert len(plan.intentions) == 3
        assert plan.rough_rhythm
        assert '此刻' in desc
