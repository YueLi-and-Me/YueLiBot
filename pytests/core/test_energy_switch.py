"""精力开关必须冻结所有入口，恢复时只计算关闭时长的基线回归。"""

from math import exp
from typing import Any

import sqlite3

import pytest

from src.core.awareness.interest import factors_for
from src.core.awareness.sleep import SleepStateController
from src.core.config.schema import Config, ScheduleConfig
from src.core.config.settings_webui import load_schema
from src.core.persona.state import ENERGY_BASELINE, ENERGY_TAU, EventDelta, Persona, PersonaState, SettlementPiece
from src.core.runtime.clock import now as current_time
from src.core.schedule.plan import DayPlanService, ScheduleSleepState
from src.core.schedule.timeline import ActivityTimeline, parse_activity_decision
from src.core.services.chat import ChatService
from src.core.services.proactive import AwarenessService

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
    result = persona.apply_elapsed(person_id, start + HOUR_MS, (
        SettlementPiece(
            activity_id=0,
            kind='rest',
            source='decided',
            started_at=start,
            ended_at=start + HOUR_MS,
            energy_rate=100.0,
            mood_rate=10.0,
        ),
    ))
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
    current = service.timeline.current(1_800_000_000_000)
    context = service.activity_decision_context(current, 1_800_000_000_000)
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


async def _noop_push(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    """吞掉事件推送；开关传播用例不校验前端投递。"""


class _PromiseStore:
    """只满足 ``AwarenessService`` 构造期恢复 promise 的最小记忆桩。"""

    def load_pending_promises(self) -> list[Any]:
        return []


class _ChatStub:
    """只提供 promise 存储的聊天桩；睡眠状态不经聊天服务读取。"""

    def __init__(self) -> None:
        self.memory = _PromiseStore()


def test_energy_switch_reload_reaches_persona_state(db: sqlite3.Connection) -> None:
    """配置改为关闭精力后，热重载必须让 ChatService 持有的 Persona 一起冻结。

    ``Persona._energy_enabled`` 是装配期拷贝的布尔值，不随配置对象引用更新；它门控的
    是带副作用的状态转换（写 ``energy_disabled_at`` 并在恢复时追补回归），因此只能由
    ``set_energy_enabled`` 传播，换引用无效。
    """

    person_id = db.execute("SELECT id FROM persons WHERE kind = 'owner'").fetchone()[0]
    now = current_time()
    db.execute('UPDATE persona_self SET energy = 60, mood = 50, updated_at = ?', (now,))
    db.commit()
    cfg = Config()
    cfg.desktop_pet.enabled = False
    chat = ChatService(db, None, None, None, _noop_push, cfg=cfg)

    disabled = cfg.model_copy(deep=True)
    disabled.schedule.energy_enabled = False
    chat.apply_config(disabled)

    assert db.execute(
        "SELECT value FROM meta WHERE key = 'energy_disabled_at'"
    ).fetchone() is not None, '关闭起点未落库，说明开关没有传播到 Persona'
    frozen = chat.persona.apply_event(
        person_id, EventDelta(favor=1, energy=-3), now + 1, weight=1.0,
    )
    assert frozen.energy == 60


def test_energy_switch_reload_reaches_sleep_controller(db: sqlite3.Connection) -> None:
    """配置改为开启精力后，热重载必须停止打断新建的睡眠段。

    ``SleepStateController._energy_enabled`` 同样是装配期拷贝。关闭态的控制器会对任何
    ``sleep`` 段调用 ``note_woken``，配置改回开启而不重启进程时，每一段睡眠都会在创建
    当轮被改写成 ``interrupted``。
    """

    db.execute(
        "INSERT INTO activities (kind, doing, mood, energy_pace, mood_pace, started_at, expected_until, source) "
        "VALUES ('sleep', '睡着', '安静', 3, 0, ?, ?, 'decided')",
        (HOUR_MS, 3 * HOUR_MS),
    )
    db.commit()
    cfg = Config()
    cfg.desktop_pet.enabled = False
    cfg.schedule.energy_enabled = False
    timeline = ActivityTimeline(db)
    service = AwarenessService(_ChatStub(), None, timeline, cfg, _noop_push)

    enabled = cfg.model_copy(deep=True)
    enabled.schedule.energy_enabled = True
    service.apply_config(enabled)

    assert service.current_sleep(2 * HOUR_MS).asleep, '开关未传播，睡眠段被立即打断'
    assert timeline.current(2 * HOUR_MS).kind == 'sleep'
    timeline.assert_invariants()


def _plan_service(db: sqlite3.Connection, *, energy_enabled: bool) -> DayPlanService:
    return DayPlanService(
        db=db, store=_Store(), persona_state=lambda: PersonaState(50, 10, 50, 0),
        interaction_density=lambda _now: '', anniversary_at=lambda: 0,
        last_interaction_at=lambda: None, generator=None,
        character_name='测试角色', character_personality='按自己的节奏生活',
        schedule_config=ScheduleConfig(energy_enabled=energy_enabled),
    )


def _insert_open_activity(db: sqlite3.Connection, kind: str, pace: int, started_at: int) -> int:
    cursor = db.execute(
        'INSERT INTO activities (kind, doing, mood, energy_pace, mood_pace, started_at, expected_until, source) '
        "VALUES (?, '去厨房弄点零食', '精力见底，想吃东西恢复元气', ?, 0, ?, ?, 'decided')",
        (kind, pace, started_at, started_at + 2 * HOUR_MS),
    )
    db.commit()
    return int(cursor.lastrowid)


@pytest.mark.parametrize(('before', 'after'), [(True, False), (False, True)])
@pytest.mark.parametrize(('kind', 'pace'), [('awake', 0), ('rest', 2)])
def test_energy_switch_reload_restarts_current_activity(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    before: bool, after: bool, kind: str, pace: int,
) -> None:
    """任一方向切换开关时，按旧开关写下的清醒／休息段立即结束，换成写入即到期的占位段。

    从开到关时旧段的 doing／mood 会带着「精力见底」逐字注入回复；从关到开时，关闭期间
    不许 sleep 的决策会一直挂到段末。两种都要求下一次读取当前活动时按新开关重新决策。
    """

    switch_at = 2 * HOUR_MS
    monkeypatch.setattr('src.core.schedule.plan.current_time', lambda: switch_at)
    service = _plan_service(db, energy_enabled=before)
    old_id = _insert_open_activity(db, kind, pace, HOUR_MS)

    service.apply_config(ScheduleConfig(energy_enabled=after))

    ended_at = db.execute('SELECT ended_at FROM activities WHERE id = ?', (old_id,)).fetchone()[0]
    assert ended_at == switch_at
    current = service.timeline.current(switch_at)
    assert current.id != old_id
    assert (current.kind, current.doing) == ('awake', '刚停下来，还没决定接下来做什么')
    assert current.started_at == current.expected_until == switch_at
    service.timeline.assert_invariants()


def test_energy_reload_without_switch_keeps_current_activity(db: sqlite3.Connection) -> None:
    """开关未变的配置重载（包括每次保存其他设置）不能打断当前活动。"""

    service = _plan_service(db, energy_enabled=False)
    old_id = _insert_open_activity(db, 'awake', 0, HOUR_MS)

    service.apply_config(ScheduleConfig(energy_enabled=False))

    assert service.timeline.current(2 * HOUR_MS).id == old_id


def test_energy_switch_leaves_sleep_to_sleep_controller(db: sqlite3.Connection) -> None:
    """sleep 段由睡眠控制器经 note_woken 结束，开关切换的重置不得抢先改写它。"""

    service = _plan_service(db, energy_enabled=True)
    db.execute(
        "INSERT INTO activities (kind, doing, mood, energy_pace, mood_pace, started_at, expected_until, source) "
        "VALUES ('sleep', '睡着', '安静', 3, 0, ?, ?, 'decided')",
        (HOUR_MS, 3 * HOUR_MS),
    )
    db.commit()

    service.apply_config(ScheduleConfig(energy_enabled=False))

    assert db.execute("SELECT COUNT(*) FROM activities WHERE kind = 'sleep' AND ended_at IS NULL").fetchone()[0] == 1
