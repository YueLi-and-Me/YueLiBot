"""生活方向必须服从用户配置与可配置人设，不预设人类作息。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import json
import sqlite3

from src.core.config.schema import ScheduleConfig
from src.core.persona.state import PersonaState
from src.core.schedule.plan import (
    DayPlanService,
    ScheduleSleepState,
    build_plan_prompt,
    describe_mood_behavior,
    parse_day_plan,
)


class _Store:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def read_json(self, key: str, fallback: Any) -> Any:
        return self.values.get(key, fallback)

    def write_json(self, key: str, value: Any) -> None:
        self.values[key] = value


class _RecordingGenerator:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.raw


def _nonhuman_plan(date: str) -> dict[str, Any]:
    return {
        'date': date,
        'theme': '今天继续巡航并校准星图',
        'intentions': [
            {'what': '穿过星门去拜访机械城', 'carriedDays': 0},
            {'what': '校准一张出现偏差的星图', 'carriedDays': 0},
            {'what': '整理沿途收到的遥测记录', 'carriedDays': 1},
        ],
        'roughRhythm': '巡航与校准交替，状态允许时再停靠',
    }


def _prompt(config: ScheduleConfig) -> str:
    return build_plan_prompt(
        date='2032-07-15',
        weekday='周四',
        occasion='没有特别节日',
        persona='当前能量稳定。',
        yesterday_theme='继续沿星图迁徙',
        unfinished_intentions='整理上一段遥测记录（已经滚了 1 天）',
        density='最近偶尔互动。',
        character_name='巡星',
        character_personality='你是沿星图迁徙的数据生命，不模仿人类日常。',
        schedule_config=config,
    )


def test_parser_accepts_configured_nonhuman_rhythm() -> None:
    raw = json.dumps(_nonhuman_plan('2032-07-15'), ensure_ascii=False)

    plan = parse_day_plan(raw, '2032-07-15', ScheduleConfig(energy_enabled=False))

    assert plan is not None
    assert plan.intentions[0].what == '穿过星门去拜访机械城'
    assert plan.intentions[2].carried_days == 1
    assert plan.rough_rhythm == '巡航与校准交替，状态允许时再停靠'


def test_schedule_prompt_uses_configured_character() -> None:
    prompt = _prompt(ScheduleConfig(energy_enabled=False))

    assert '「巡星」' in prompt
    assert '沿星图迁徙的数据生命' in prompt
    assert '当前不允许实际活动进入睡眠' in prompt
    assert '测试角色' not in prompt
    assert 'slots' not in prompt
    assert 'bedtimeHint' not in prompt


async def test_service_passes_configured_character_to_generator(
    db: sqlite3.Connection,
) -> None:
    date = '2032-07-15'
    generator = _RecordingGenerator(
        json.dumps(_nonhuman_plan(date), ensure_ascii=False)
    )
    service = DayPlanService(
        db=db,
        store=_Store(),
        persona_state=lambda: PersonaState(50.0, 60.0, 50.0, 0),
        interaction_density=lambda _now: '最近偶尔互动。',
        anniversary_at=lambda: 0,
        last_interaction_at=lambda: None,
        generator=generator,
        character_name='巡星',
        character_personality='你是沿星图迁徙的数据生命。',
        schedule_config=ScheduleConfig(energy_enabled=False),
    )

    plan = await service.ensure(
        int(datetime(2032, 7, 15, 12, 0).timestamp() * 1000)
    )

    assert plan.theme == '今天继续巡航并校准星图'
    assert len(generator.prompts) == 1
    assert '沿星图迁徙的数据生命' in generator.prompts[0]


def test_disabled_sleep_never_enters_sleep_state() -> None:
    prompt = _prompt(ScheduleConfig(energy_enabled=False))

    assert '当前不允许实际活动进入睡眠' in prompt
    assert '不要承诺睡觉时刻' in prompt


def test_activity_mood_is_behavior_background_instead_of_announcement() -> None:
    """状态只影响行为；除非被问或要离场，否则不能主动复读。"""
    prompt = describe_mood_behavior('有点困')

    assert '状态影响语气与反应速度即可' in prompt
    assert '不要主动把它说出来' in prompt
    assert '除非对方问起' in prompt
    assert '确实要因此结束对话' in prompt
    assert '让它自然影响反应' not in prompt


def test_day_plan_marks_activity_as_background_not_topic(
    db: sqlite3.Connection,
) -> None:
    """当前活动只在用户询问时展开，不让方向变成每轮播报的话题。"""

    now = int(datetime(2026, 8, 19, 22, 31).timestamp() * 1000)
    db.execute(
        '''INSERT INTO activities
             (kind, doing, mood, energy_pace, mood_pace, advances,
              started_at, expected_until, ended_at, source)
           VALUES ('awake', ?, ?, 0, 0, NULL, ?, ?, NULL, 'decided')''',
        ('合上电脑，收拾书桌', '温柔且内省', now, now + 30 * 60_000),
    )
    db.commit()
    service = DayPlanService(
        db=db,
        store=_Store(),
        persona_state=lambda: PersonaState(50.0, 60.0, 50.0, now),
        interaction_density=lambda _now: '最近偶尔互动。',
        anniversary_at=lambda: 0,
        last_interaction_at=lambda: None,
        generator=None,
        character_name='测试角色',
        character_personality='按自己的节奏生活。',
    )

    background = service.describe(now, ScheduleSleepState(asleep=False))
    asked = service.describe(
        now,
        ScheduleSleepState(asleep=False),
        include_activity=True,
    )

    assert '合上电脑' not in background
    assert '合上电脑' in asked
    assert '不用展开讲' in asked
