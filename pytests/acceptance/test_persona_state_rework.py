"""精力、心情与统一状态重做的可执行验收断言。

本文件对应 开发文档 persona-state-rework.md（不随代码分发） 的单元、迁移、提示词和跨语言合同。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from inspect import signature
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence, Tuple

import json
import random
import sqlite3

import pytest

from src.core.db import schema as db_schema
from src.core.db.migrations import manager as migration_manager
from src.core.db.migrations import v12_to_v13
from src.core.db.migrations.manager import run_migrations
from src.core.config.schema import ScheduleConfig
from src.core.persona import state as persona_state
from src.core.schedule import plan as schedule_plan


LEGACY_SCHEDULE = pytest.mark.skip(
    reason='旧时刻表 pace 与概率睡眠契约已由活动时间线取代',
)


HOUR_MS = 3_600_000
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _Store:
    """为日程服务提供隔离的内存 JSON 存储。"""

    def __init__(self) -> None:
        self.values: Dict[str, Any] = {}

    def read_json(self, key: str, fallback: Any) -> Any:
        return self.values.get(key, fallback)

    def write_json(self, key: str, value: Any) -> None:
        self.values[key] = value


def _timestamp(date: str, hour: int, minute: int = 0) -> int:
    """把本地日期与时刻转换成毫秒时间戳。"""

    return int(datetime.fromisoformat(f'{date} {hour:02d}:{minute:02d}').timestamp() * 1000)


def _state(
    *,
    intimacy: float = 73.0,
    energy: float = 60.0,
    mood: float = 50.0,
    updated_at: int = 0,
) -> Any:
    """同时兼容改动前后的 ``PersonaState`` 构造，用于取得逐条红灯。"""

    values: Dict[str, Any] = {
        'intimacy': intimacy,
        'energy': energy,
        'updated_at': updated_at,
    }
    if 'mood' in persona_state.PersonaState.__dataclass_fields__:
        values['mood'] = mood
    return persona_state.PersonaState(**values)


def _plan_dict(
    date: str,
    slots: Sequence[Tuple[str, int, int]],
    *,
    energy_enabled: bool,
    bedtime: str,
    wake: str,
    include_paces: bool = True,
) -> Dict[str, Any]:
    """构造一份合法日程；可显式省略两个 pace 模拟历史存量。"""

    raw_slots: List[Dict[str, Any]] = []
    for index, (from_time, energy_pace, mood_pace) in enumerate(slots):
        item: Dict[str, Any] = {
            'from': from_time,
            'doing': f'处理第{index + 1}段自己的事情',
            'mood': f'第{index + 1}段状态自然',
        }
        if include_paces:
            item['energyPace'] = energy_pace
            item['moodPace'] = mood_pace
        raw_slots.append(item)
    return {
        'date': date,
        'slots': raw_slots,
        'bedtimeHint': bedtime,
        'wakeHint': wake,
        'theme': '按自己的节奏安排一天',
        'carryOver': '无',
        'sleepEnabled': energy_enabled,
        'bedtimeDayBoundary': '02:00',
    }


def _make_schedule(
    store: _Store,
    *,
    energy_enabled: bool,
    state_provider: Callable[[], Any] | None = None,
) -> Any:
    """按当前构造签名装配日程服务，避免改动前在收集阶段统一报错。"""

    provider = state_provider or (lambda: _state())
    config = ScheduleConfig(
        min_slots=1,
        max_slots=24,
        energy_enabled=energy_enabled,
        fallback_bedtime='20:00',
        fallback_wake='08:00',
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


def _save_plan(store: _Store, plan: Dict[str, Any]) -> None:
    store.values[f"day_plan:{plan['date']}"] = plan


def _energy_delta(service: Any, from_ms: int, to_ms: int) -> float:
    """读取当前实现的时间积分，旧实现用于给解耦断言提供真实数值红灯。"""

    integrate = getattr(service, 'integrate_between', None)
    if integrate is not None:
        return float(integrate(from_ms, to_ms).energy_delta)
    rest_hours = service.rest_hours_between(from_ms, to_ms)
    hours = (to_ms - from_ms) / HOUR_MS
    return rest_hours * 4.0 - (hours - rest_hours) * 2.0


@LEGACY_SCHEDULE
def test_disabled_sleep_still_recovers_energy_across_rest_window() -> None:
    """关闭自动睡眠只禁止下线，不能再切断 bedtime/wake 的精力恢复。"""

    store = _Store()
    _save_plan(
        store,
        _plan_dict(
            '2032-07-15',
            [('08:00', 0, 0)],
            energy_enabled=False,
            bedtime='20:00',
            wake='08:00',
        ),
    )
    service = _make_schedule(store, energy_enabled=False)

    delta = _energy_delta(
        service,
        _timestamp('2032-07-15', 20),
        _timestamp('2032-07-16', 8),
    )

    assert delta > 0.0, f'关闭自动睡眠后跨休息窗口仍得到 energy_delta={delta}'


@LEGACY_SCHEDULE
def test_disabled_sleep_never_enters_sleep_state() -> None:
    """解耦恢复不能反向打开用户关闭的睡眠状态机。"""

    result = evaluate_sleep(
        SleepInputs(
            date='2032-07-15',
            bedtime_hint='20:00',
            wake_hint='08:00',
            energy=0.0,
            last_interaction_at=None,
            energy_enabled=False,
            bedtime_day_boundary='02:00',
        ),
        _timestamp('2032-07-15', 23),
        previously_asleep=True,
    )

    assert result.asleep is False
    assert result.drowsy is False
    assert result.probability == 0.0


@LEGACY_SCHEDULE
def test_rest_window_can_reach_second_day_after_plan_date() -> None:
    """日期边界把入睡推到次日时，后一天清晨的休息尾段不能漏算。"""

    store = _Store()
    _save_plan(
        store,
        _plan_dict(
            '2032-07-15',
            [('00:00', 0, 0)],
            energy_enabled=False,
            bedtime='02:00',
            wake='01:00',
        ),
    )
    store.values['day_plan:2032-07-15']['bedtimeDayBoundary'] = '02:00'
    for date in ('2032-07-16', '2032-07-17'):
        _save_plan(
            store,
            _plan_dict(
                date,
                [('00:00', 0, 0)],
                energy_enabled=False,
                bedtime='10:00',
                wake='11:00',
            ),
        )
    service = _make_schedule(store, energy_enabled=False)

    effect = service.integrate_between(
        _timestamp('2032-07-17', 0),
        _timestamp('2032-07-17', 1),
    )

    assert effect.energy_delta == pytest.approx(4.0)


@LEGACY_SCHEDULE
def test_legacy_plan_without_paces_keeps_hourly_energy_curve() -> None:
    """旧格式日程缺 pace 时，二十四小时每个整点都保持旧公式结果。"""

    store = _Store()
    _save_plan(
        store,
        _plan_dict(
            '2032-07-15',
            [('08:00', 0, 0)],
            energy_enabled=True,
            bedtime='20:00',
            wake='08:00',
            include_paces=False,
        ),
    )
    service = _make_schedule(store, energy_enabled=True)
    integrate = getattr(service, 'integrate_between', None)
    assert integrate is not None, 'DayPlanService 尚未提供 integrate_between'
    start = _timestamp('2032-07-15', 8)

    for elapsed_hours in range(1, 25):
        effect = integrate(start, start + elapsed_hours * HOUR_MS)
        awake_hours = min(float(elapsed_hours), 12.0)
        rest_hours = max(0.0, float(elapsed_hours) - 12.0)
        expected = -2.0 * awake_hours + 4.0 * rest_hours
        assert effect.energy_delta == pytest.approx(expected, abs=1e-9)
        assert effect.mood_delta == pytest.approx(0.0, abs=1e-9)


@LEGACY_SCHEDULE
def test_day_plan_pace_parsing_and_round_trip_contract() -> None:
    """pace 缺失/类型错误回零，越界整数拒收，合法整数完整序列化。"""

    config = ScheduleConfig(min_slots=1, max_slots=4)
    missing = _plan_dict(
        '2032-07-15',
        [('08:00', 0, 0)],
        energy_enabled=True,
        bedtime='20:00',
        wake='08:00',
        include_paces=False,
    )
    parsed_missing = schedule_plan.parse_day_plan(
        json.dumps(missing, ensure_ascii=False),
        '2032-07-15',
        config,
    )
    assert parsed_missing is not None
    assert parsed_missing.slots[0].energy_pace == 0
    assert parsed_missing.slots[0].mood_pace == 0

    invalid_types = _plan_dict(
        '2032-07-15',
        [('08:00', 0, 0)],
        energy_enabled=True,
        bedtime='20:00',
        wake='08:00',
    )
    invalid_types['slots'][0]['energyPace'] = True
    invalid_types['slots'][0]['moodPace'] = 1.5
    parsed_types = schedule_plan.parse_day_plan(
        json.dumps(invalid_types, ensure_ascii=False),
        '2032-07-15',
        config,
    )
    assert parsed_types is not None
    assert parsed_types.slots[0].energy_pace == 0
    assert parsed_types.slots[0].mood_pace == 0

    for field in ('energyPace', 'moodPace'):
        out_of_range = _plan_dict(
            '2032-07-15',
            [('08:00', 0, 0)],
            energy_enabled=True,
            bedtime='20:00',
            wake='08:00',
        )
        out_of_range['slots'][0][field] = 4
        assert schedule_plan.parse_day_plan(
            json.dumps(out_of_range, ensure_ascii=False),
            '2032-07-15',
            config,
        ) is None

    valid = _plan_dict(
        '2032-07-15',
        [('08:00', -3, 2)],
        energy_enabled=True,
        bedtime='20:00',
        wake='08:00',
    )
    parsed_valid = schedule_plan.parse_day_plan(
        json.dumps(valid, ensure_ascii=False),
        '2032-07-15',
        config,
    )
    assert parsed_valid is not None
    serialized = schedule_plan._plan_to_dict(parsed_valid)
    assert serialized['slots'][0]['energyPace'] == -3
    assert serialized['slots'][0]['moodPace'] == 2


@LEGACY_SCHEDULE
def test_energy_pace_normalization_preserves_fourteen_hour_total() -> None:
    """一百组可复现 pace 只能改变形状，十四小时总量恒为负二十八。"""

    rng = random.Random(20260824)
    slot_times = ['08:00', '10:00', '12:00', '14:00', '16:00', '18:00', '20:00']
    for sample in range(100):
        paces = [rng.randint(-3, 3) for _ in slot_times]
        store = _Store()
        _save_plan(
            store,
            _plan_dict(
                '2032-07-15',
                [(time, pace, 0) for time, pace in zip(slot_times, paces)],
                energy_enabled=False,
                bedtime='22:00',
                wake='08:00',
            ),
        )
        service = _make_schedule(store, energy_enabled=False)
        integrate = getattr(service, 'integrate_between', None)
        assert integrate is not None, 'DayPlanService 尚未提供 integrate_between'

        effect = integrate(
            _timestamp('2032-07-15', 8),
            _timestamp('2032-07-15', 22),
        )

        assert effect.energy_delta == pytest.approx(-28.0, abs=1e-8), sample


@LEGACY_SCHEDULE
def test_opposite_energy_and_mood_paces_move_axes_in_opposite_directions() -> None:
    """同一活动可让精力与心情反向变化，两根轴不能互相推导。"""

    store = _Store()
    _save_plan(
        store,
        _plan_dict(
            '2032-07-15',
            [
                ('08:00', -3, 2),
                ('11:00', -1, -2),
                ('14:00', 1, 2),
                ('17:00', 3, -1),
            ],
            energy_enabled=False,
            bedtime='20:00',
            wake='08:00',
        ),
    )
    service = _make_schedule(store, energy_enabled=False)
    integrate = getattr(service, 'integrate_between', None)
    assert integrate is not None, 'DayPlanService 尚未提供 integrate_between'

    draining_but_happy = integrate(
        _timestamp('2032-07-15', 8),
        _timestamp('2032-07-15', 11),
    )
    restoring_but_low = integrate(
        _timestamp('2032-07-15', 17),
        _timestamp('2032-07-15', 20),
    )

    assert draining_but_happy.energy_delta < 0.0
    assert draining_but_happy.mood_delta > 0.0
    assert restoring_but_low.energy_delta > 0.0
    assert restoring_but_low.mood_delta < 0.0


def test_positive_mood_schedule_converges_below_one_hundred(
    db: sqlite3.Connection,
) -> None:
    """持续正向心情输入在回归项作用下收敛，不会卡到上边界。"""

    effect_type = getattr(persona_state, 'ElapsedEffect', None)
    assert effect_type is not None, 'persona.state 尚未提供 ElapsedEffect'
    persona = persona_state.Persona(db)
    start = _timestamp('2032-07-15', 8)
    db.execute(
        'UPDATE persona_bond SET updated_at = ? WHERE person_id = 1',
        (start,),
    )
    db.execute(
        'UPDATE persona_self SET energy = ?, mood = ?, updated_at = ? WHERE id = 1',
        (50.0, 50.0, start),
    )
    db.commit()

    daily_moods: List[float] = []
    for hour_index in range(1, 7 * 24 + 1):
        current = persona.apply_elapsed(
            1,
            start + hour_index * HOUR_MS,
            effect_type(energy_delta=0.0, mood_delta=6.0),
        )
        if hour_index % 24 == 0:
            daily_moods.append(current.mood)

    assert all(50.0 < mood < 100.0 for mood in daily_moods)
    assert daily_moods[-1] < 100.0
    assert abs(daily_moods[-1] - daily_moods[-2]) < abs(
        daily_moods[1] - daily_moods[0]
    )


@pytest.mark.parametrize('energy_enabled', [False, True])
def test_spent_energy_status_is_backend_derived(
    energy_enabled: bool,
) -> None:
    """精力见底且醒着时统一标签必须是“精疲力尽”，与睡眠开关无关。"""

    label_fn = getattr(persona_state, 'status_label', None)
    assert label_fn is not None, 'persona.state 尚未提供统一 status_label'
    label = label_fn(
        _state(energy=0.0, mood=50.0),
        asleep=False,
        just_woke=False,
        resting=False,
    )

    assert label == '精疲力尽'


def test_persona_and_planning_descriptions_never_expose_numbers() -> None:
    """关系口径只留关系；规划口径给负反馈，两者都不暴露状态数值。"""

    planning = getattr(persona_state, 'describe_persona_for_planning', None)
    assert planning is not None, 'persona.state 尚未提供 describe_persona_for_planning'
    for energy in (0.0, 19.0, 20.0, 44.0, 45.0, 86.0, 100.0):
        for mood in (0.0, 34.0, 35.0, 64.0, 65.0, 100.0):
            state = _state(energy=energy, mood=mood)
            relationship = persona_state.describe_persona(state)
            plan_guidance = planning(state)
            assert not any(char.isdigit() for char in relationship)
            assert not any(char.isdigit() for char in plan_guidance)
            assert '精力' not in relationship
            assert '困' not in relationship
            assert '心情' not in relationship

    low_guidance = planning(_state(energy=0.0, mood=10.0))
    assert '至少有两段' in low_guidance
    assert '不要把一整天都写成没劲' in low_guidance


def test_v12_to_v13_preserves_rows_and_backfills_neutral_mood(
    tmp_path: Path,
) -> None:
    """旧状态值逐行不变，两张表的历史空白心情统一回填五十。"""

    path = tmp_path / 'memory.db'
    db = sqlite3.connect(str(path))
    try:
        db.executescript(db_schema.DDL)
        db.executescript(db_schema.SEED)
        db.execute('DROP INDEX IF EXISTS idx_persona_snapshots_time')
        db.execute('DROP TABLE persona_snapshots')
        db.execute('DROP TABLE persona_self')
        db.executescript(
            '''
            CREATE TABLE persona_self (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              energy REAL NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE TABLE persona_snapshots (
              date TEXT PRIMARY KEY,
              intimacy REAL NOT NULL,
              energy REAL NOT NULL,
              captured_at INTEGER NOT NULL
            );
            CREATE INDEX idx_persona_snapshots_time
              ON persona_snapshots(captured_at DESC);
            '''
        )
        db.execute(
            'INSERT INTO persona_self (id, energy, updated_at) VALUES (1, 73.25, 111)',
        )
        db.execute(
            'UPDATE persona_bond SET intimacy = 44.5, updated_at = 112 WHERE person_id = 1',
        )
        db.executemany(
            '''INSERT INTO persona_snapshots (date, intimacy, energy, captured_at)
               VALUES (?, ?, ?, ?)''',
            [
                ('2032-07-13', 41.0, 72.0, 101),
                ('2032-07-14', 42.0, 71.0, 102),
            ],
        )
        db.execute('PRAGMA user_version = 12')
        db.commit()
        before_self = db.execute(
            'SELECT energy, updated_at FROM persona_self WHERE id = 1'
        ).fetchone()
        before_bond = db.execute(
            'SELECT intimacy, updated_at FROM persona_bond WHERE person_id = 1'
        ).fetchone()
        before_snapshots = db.execute(
            '''SELECT date, intimacy, energy, captured_at
               FROM persona_snapshots ORDER BY date'''
        ).fetchall()

        run_migrations(db, path)

        # 断言链条跑到了头，而不是把头版本号写死：本用例验的是这一步迁移
        # 保住了数据，后面每加一步迁移都要来改一次数字才是错的。
        assert db.execute('PRAGMA user_version').fetchone() == (
            migration_manager.CURRENT_VERSION,
        )
        assert db.execute(
            'SELECT energy, updated_at FROM persona_self WHERE id = 1'
        ).fetchone() == before_self
        assert db.execute(
            'SELECT intimacy, updated_at FROM persona_bond WHERE person_id = 1'
        ).fetchone() == before_bond
        assert db.execute(
            '''SELECT date, intimacy, energy, captured_at
               FROM persona_snapshots ORDER BY date'''
        ).fetchall() == before_snapshots
        assert db.execute('SELECT mood FROM persona_self WHERE id = 1').fetchone() == (50.0,)
        assert db.execute(
            'SELECT mood FROM persona_snapshots ORDER BY date'
        ).fetchall() == [(50.0,), (50.0,)]
        assert db.execute('SELECT COUNT(*) FROM activities').fetchone() == (0,)
    finally:
        db.close()

    backups = list((tmp_path / 'backups').glob('memory.v12.*.db'))
    assert len(backups) == 1
    backup = sqlite3.connect(str(backups[0]))
    try:
        assert backup.execute('PRAGMA user_version').fetchone() == (12,)
        assert 'mood' not in {
            str(row[1])
            for row in backup.execute("PRAGMA table_info('persona_self')")
        }
        assert backup.execute(
            'SELECT energy, updated_at FROM persona_self WHERE id = 1'
        ).fetchone() == (73.25, 111)
    finally:
        backup.close()


def test_v12_to_v13_rejects_preexisting_mood_with_wrong_constraints() -> None:
    """同名列若约束或默认值错误，迁移必须暴露漂移而不是静默标成 v13。"""

    db = sqlite3.connect(':memory:')
    try:
        db.executescript(
            '''
            CREATE TABLE persona_self (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              energy REAL NOT NULL,
              updated_at INTEGER NOT NULL,
              mood REAL
            );
            CREATE TABLE persona_snapshots (
              date TEXT PRIMARY KEY,
              intimacy REAL NOT NULL,
              energy REAL NOT NULL,
              captured_at INTEGER NOT NULL,
              mood REAL DEFAULT 0
            );
            INSERT INTO persona_self VALUES (1, 70.0, 111, NULL);
            INSERT INTO persona_snapshots VALUES ('2032-07-14', 40.0, 60.0, 112, 17.0);
            '''
        )

        with pytest.raises(RuntimeError, match='人格状态结构不符合预期'):
            v12_to_v13.migrate(db)

        assert db.execute('SELECT mood FROM persona_self').fetchone() == (None,)
        assert db.execute('SELECT mood FROM persona_snapshots').fetchone() == (17.0,)
    finally:
        db.close()


def test_webui_and_ipc_consume_backend_status_and_mood_contract() -> None:
    """前端只读后端状态标签，并同步 mood 与两个 pace 的跨语言类型。"""

    status_strip = (PROJECT_ROOT / 'webui/src/features/observe/StatusStrip.tsx').read_text(
        encoding='utf-8'
    )
    snapshot_sections = (
        PROJECT_ROOT / 'webui/src/features/observe/SnapshotSections.tsx'
    ).read_text(encoding='utf-8')
    ipc = (PROJECT_ROOT / 'electron/shared/ipc.ts').read_text(encoding='utf-8')

    assert 'payload.selfState.statusLabel' in status_strip
    assert 'const sleepLabel' not in status_strip
    assert 'payload.selfState.statusLabel' in snapshot_sections
    assert 'label="心情"' in snapshot_sections
    assert 'mood: number' in ipc
    assert 'statusLabel: string' in ipc
    assert 'energyPace: number' in ipc
    assert 'moodPace: number' in ipc
