"""使用固定首轮日程连续模拟三天的人格状态曲线。"""

from __future__ import annotations

from datetime import datetime, timedelta
from inspect import signature
from itertools import pairwise
from typing import Any, Callable, Dict, List, Tuple

import json
import sqlite3

import pytest

from src.core.config.schema import ScheduleConfig
from src.core.persona import state as persona_state
from src.core.schedule import plan as schedule_plan


pytestmark = pytest.mark.skip(
    reason='旧时刻表驱动的三日曲线已退役；活动时间线三日断言见 test_life_loop_rework.py',
)


HOUR_MS = 3_600_000
TEN_MINUTES_MS = 10 * 60_000


class _Store:
    """连续模拟使用的固定日程存储。"""

    def __init__(self) -> None:
        self.values: Dict[str, Any] = {}

    def read_json(self, key: str, fallback: Any) -> Any:
        return self.values.get(key, fallback)

    def write_json(self, key: str, value: Any) -> None:
        self.values[key] = value


def _timestamp(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _state_provider(persona: Any) -> Callable[[], Any]:
    return lambda: persona.get(1)


def _fixed_plan(date: str) -> Dict[str, Any]:
    """返回一次写死后不重抽样的完整计划，清醒段 pace 加权均值为零。"""

    slots: List[Tuple[str, int, int]] = [
        ('07:00', -3, 2),
        ('09:00', -2, 1),
        ('11:00', 3, 2),
        ('13:00', -1, -1),
        ('15:00', 3, 2),
        ('17:00', 0, 0),
        ('19:00', 2, -1),
        ('21:00', -2, -2),
    ]
    return {
        'date': date,
        'slots': [
            {
                'from': from_time,
                'doing': f'按计划处理第{index + 1}段活动',
                'mood': f'第{index + 1}段自然变化',
                'energyPace': energy_pace,
                'moodPace': mood_pace,
            }
            for index, (from_time, energy_pace, mood_pace) in enumerate(slots)
        ],
        'bedtimeHint': '23:00',
        'wakeHint': '07:00',
        'theme': '按自己的节奏忙一阵再歇一阵',
        'carryOver': '无',
        'sleepEnabled': True,
        'bedtimeDayBoundary': '02:00',
    }


def _make_schedule(store: _Store, provider: Callable[[], Any]) -> Any:
    config = ScheduleConfig(
        min_slots=1,
        max_slots=24,
        energy_enabled=True,
        fallback_bedtime='23:00',
        fallback_wake='07:00',
    )
    kwargs: Dict[str, Any] = {
        'store': store,
        'interaction_density': lambda _now: '最近偶尔互动。',
        'anniversary_at': lambda: 0,
        'last_interaction_at': lambda: None,
        'character_name': '测试角色',
        'character_personality': '按自己的节奏生活。',
        'generator': None,
        'schedule_config': config,
    }
    parameters = signature(schedule_plan.DayPlanService).parameters
    if 'persona_state' in parameters:
        kwargs['persona_state'] = provider
    else:
        kwargs['persona_description'] = lambda: '当前状态平稳。'
        kwargs['energy'] = lambda: provider().energy
    return schedule_plan.DayPlanService(**kwargs)


def _daily_groups(points: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for point in points:
        grouped.setdefault(point['date'], []).append(point)
    return grouped


def _longest_zero_run_minutes(points: List[Dict[str, Any]]) -> int:
    longest = 0
    current = 0
    for point in points:
        if point['energy'] <= 1e-9:
            current += 10
            longest = max(longest, current)
        else:
            current = 0
    return longest


def test_three_day_curve_has_true_intraday_waves_and_never_stalls_at_zero(
    db: sqlite3.Connection,
) -> None:
    """固定三天一次跑完：每天有升有降且极差足够，精力不在零点卡住。"""

    integrate_name = getattr(schedule_plan.DayPlanService, 'integrate_between', None)
    assert integrate_name is not None, 'DayPlanService 尚未提供 integrate_between'
    assert 'mood' in persona_state.PersonaState.__dataclass_fields__

    persona = persona_state.Persona(db)
    start_dt = datetime(2032, 7, 15, 0, 0)
    start = _timestamp(start_dt)
    db.execute(
        'UPDATE persona_bond SET updated_at = ? WHERE person_id = 1',
        (start,),
    )
    db.execute(
        'UPDATE persona_self SET energy = ?, mood = ?, updated_at = ? WHERE id = 1',
        (60.0, 50.0, start),
    )
    db.commit()

    store = _Store()
    plans: List[Dict[str, Any]] = []
    for day_offset in range(-1, 4):
        date = (start_dt + timedelta(days=day_offset)).strftime('%Y-%m-%d')
        plan = _fixed_plan(date)
        store.values[f'day_plan:{date}'] = plan
        if 0 <= day_offset < 3:
            plans.append(plan)
    service = _make_schedule(store, _state_provider(persona))

    print('PERSONA_STATE_DAY_PLANS_BEGIN')
    print(json.dumps(plans, ensure_ascii=False, indent=2))
    print('PERSONA_STATE_DAY_PLANS_END')
    print('PERSONA_STATE_CURVE_BEGIN')

    points: List[Dict[str, Any]] = []
    transitions: List[Dict[str, Any]] = []
    previously_asleep = False
    sleep_started_at: int | None = None
    # 终点午夜属于第四个自然日；三日曲线取到第三天 23:50，避免多出一个单点分组。
    for step in range(1, 3 * 24 * 6):
        now = start + step * TEN_MINUTES_MS
        before = persona.get(1)
        effect = service.integrate_between(before.updated_at, now)
        after = persona.apply_elapsed(1, now, effect)
        now_dt = datetime.fromtimestamp(now / 1000)
        sleep = evaluate_sleep(
            SleepInputs(**service.sleep_inputs(now)),
            now,
            previously_asleep=previously_asleep,
            jitter=0.5,
            sleep_started_at=sleep_started_at,
        )
        if sleep.asleep != previously_asleep:
            transition = {
                'kind': '入睡' if sleep.asleep else '自然醒',
                'at': now_dt.strftime('%Y-%m-%d %H:%M'),
                'p': round(sleep.probability, 4),
                'energy': round(after.energy, 4),
                'mood': round(after.mood, 4),
            }
            transitions.append(transition)
            print(json.dumps(transition, ensure_ascii=False))
            sleep_started_at = now if sleep.asleep else None
        previously_asleep = sleep.asleep
        point = {
            'at': now_dt.strftime('%Y-%m-%d %H:%M'),
            'date': now_dt.strftime('%Y-%m-%d'),
            'p': sleep.probability,
            'asleep': sleep.asleep,
            'energy': round(after.energy, 4),
            'mood': round(after.mood, 4),
        }
        points.append(point)
        if step % 6 == 0:
            print(json.dumps(point, ensure_ascii=False))
    print('PERSONA_STATE_CURVE_END')

    grouped = _daily_groups(points)
    assert len(grouped) == 3
    for date, daily_points in grouped.items():
        energies = [float(point['energy']) for point in daily_points]
        assert max(energies) - min(energies) >= 20.0, date
        assert any(after > before for before, after in pairwise(energies)), date
        assert any(after < before for before, after in pairwise(energies)), date
    assert _longest_zero_run_minutes(points) <= 120
    assert [transition['kind'] for transition in transitions] == [
        '入睡',
        '自然醒',
        '入睡',
        '自然醒',
        '入睡',
        '自然醒',
        '入睡',
    ]
