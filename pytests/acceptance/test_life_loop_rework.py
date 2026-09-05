"""生活循环重做的本地可执行验收断言。

本文件对应 ``docs/life-loop-rework.md`` §七中所有无需真实模型的星标条目。
"""

from __future__ import annotations

from datetime import datetime
from importlib import import_module
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

import asyncio
import json
import sqlite3

import pytest

from src.core.agent.action_protocol import GateInputFacts
from src.core.agent.conversation_gate import GateRequest, decide_disposition
from src.core.awareness.budget import InterruptContext, ProactiveState, decide
from src.core.common.db import schema as db_schema
from src.core.common.db.migrations import manager as migration_manager
from src.core.common.db.migrations.manager import run_migrations
from src.core.config.schema import ScheduleConfig
from src.core.persona.state import PersonaState, status_label
from src.core.schedule import plan as schedule_plan


HOUR_MS = 3_600_000
MINUTE_MS = 60_000
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _PlanStore:
    """为每日方向生成提供隔离的 JSON 存储。"""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def read_json(self, key: str, fallback: Any) -> Any:
        return self.values.get(key, fallback)

    def write_json(self, key: str, value: Any) -> None:
        self.values[key] = value


def _timeline_module() -> Any:
    """延迟导入新模块，使改动前每个验收节点都能独立留下失败信息。"""

    try:
        return import_module('src.core.schedule.timeline')
    except ModuleNotFoundError as exc:
        pytest.fail(f'尚未实现活动时间线模块：{exc}')


def _database() -> sqlite3.Connection:
    """创建包含当前 DDL 的独立内存库。"""

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript(db_schema.DDL)
    db.executescript(db_schema.SEED)
    db.commit()
    return db


def _timestamp(date: str, hour: int, minute: int = 0) -> int:
    return int(datetime.fromisoformat(f'{date} {hour:02d}:{minute:02d}').timestamp() * 1000)


def _gate_request(stream_kind: str, **overrides: Any) -> GateRequest:
    """构造字段完整的门控输入，让参数化用例只突出被验证的差异。"""

    values: dict[str, Any] = {
        'stream_kind': stream_kind,
        'mentioned_me': False,
        'name_mentioned': False,
        'asleep': False,
        'at_mention_must_reply': False,
        'replies_in_window': 0,
        'max_replies_in_window': 10,
    }
    values.update(overrides)
    return GateRequest(**values)


def _draft(
    module: Any,
    *,
    kind: str = 'awake',
    doing: str = '整理手头的东西',
    mood: str = '专注但仍然会回应',
    energy_pace: int = 0,
    mood_pace: int = 0,
    minutes: int = 30,
    advances: int | None = None,
) -> Any:
    return module.ActivityDraft(
        kind=kind,
        doing=doing,
        mood=mood,
        energy_pace=energy_pace,
        mood_pace=mood_pace,
        minutes=minutes,
        advances=advances,
    )


def _insert_activity(
    db: sqlite3.Connection,
    *,
    kind: str,
    doing: str,
    mood: str,
    energy_pace: int,
    mood_pace: int,
    started_at: int,
    expected_until: int,
    ended_at: int | None,
    source: str = 'decided',
    advances: int | None = None,
) -> int:
    cursor = db.execute(
        '''INSERT INTO activities
             (kind, doing, mood, energy_pace, mood_pace, advances,
              started_at, expected_until, ended_at, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            kind,
            doing,
            mood,
            energy_pace,
            mood_pace,
            advances,
            started_at,
            expected_until,
            ended_at,
            source,
        ),
    )
    db.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


async def _allow_background_task() -> None:
    """给 ``current()`` 创建的后台决策任务两轮事件循环执行机会。"""

    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_kind_energy_pace_is_clamped_and_warned(capsys: pytest.CaptureFixture[str]) -> None:
    """★ 三类活动各自限幅；越界写入会给出 warning。"""

    module = _timeline_module()
    cases = (('awake', 3, 1), ('rest', -3, 1), ('sleep', 9, 3))
    for index, (kind, raw_pace, expected) in enumerate(cases):
        db = _database()
        try:
            now = _timestamp('2032-07-15', 10 + index)
            _insert_activity(
                db,
                kind='awake',
                doing='等待下一步',
                mood='状态平稳',
                energy_pace=0,
                mood_pace=0,
                started_at=now - HOUR_MS,
                expected_until=now,
                ended_at=None,
            )

            async def decide_next(_activity: Any, _now: int, _gap_ms: int) -> Any:
                return module.ActivityTransition(
                    next_activity=_draft(
                        module,
                        kind=kind,
                        energy_pace=raw_pace,
                    ),
                )

            timeline = module.ActivityTimeline(db, decider=decide_next)
            timeline.current(now)
            await _allow_background_task()
            row = db.execute(
                'SELECT energy_pace FROM activities ORDER BY id DESC LIMIT 1'
            ).fetchone()
            assert row is not None and row[0] == expected
        finally:
            db.close()

    assert 'energyPace' in capsys.readouterr().out


def test_activity_timeline_invariants_detect_overlap_gap_and_open_row_order() -> None:
    """★ 时间线不重叠、不留洞，且只能由最后一条保持进行中。"""

    module = _timeline_module()
    db = _database()
    try:
        start = _timestamp('2032-07-15', 8)
        first = _insert_activity(
            db,
            kind='awake',
            doing='看书',
            mood='很专注',
            energy_pace=0,
            mood_pace=0,
            started_at=start,
            expected_until=start + HOUR_MS,
            ended_at=start + HOUR_MS,
        )
        _insert_activity(
            db,
            kind='rest',
            doing='躺一会儿',
            mood='慢慢放松下来',
            energy_pace=1,
            mood_pace=1,
            started_at=start + HOUR_MS,
            expected_until=start + 2 * HOUR_MS,
            ended_at=None,
        )
        timeline = module.ActivityTimeline(db)
        timeline.assert_invariants()

        db.execute('UPDATE activities SET started_at = ? WHERE id = ?', (start + HOUR_MS + 1, first + 1))
        db.commit()
        with pytest.raises(RuntimeError, match='不留洞'):
            timeline.assert_invariants()

        db.execute('UPDATE activities SET started_at = ? WHERE id = ?', (start + HOUR_MS - 1, first + 1))
        db.commit()
        with pytest.raises(RuntimeError, match='不重叠'):
            timeline.assert_invariants()

        db.execute('UPDATE activities SET started_at = ?, ended_at = NULL WHERE id = ?', (start + HOUR_MS, first))
        db.commit()
        with pytest.raises(RuntimeError, match='进行中'):
            timeline.assert_invariants()
    finally:
        db.close()


def test_activity_integration_is_deterministic_and_balanced() -> None:
    """★ 醒 16 小时 pace=0 与睡 8 小时 pace=3 的净精力变化恒为零。"""

    module = _timeline_module()
    db = _database()
    try:
        start = _timestamp('2032-07-15', 8)
        sleep_at = start + 16 * HOUR_MS
        end = sleep_at + 8 * HOUR_MS
        _insert_activity(
            db,
            kind='awake',
            doing='处理一天里的事情',
            mood='状态平稳',
            energy_pace=0,
            mood_pace=-1,
            started_at=start,
            expected_until=sleep_at,
            ended_at=sleep_at,
        )
        _insert_activity(
            db,
            kind='sleep',
            doing='睡觉',
            mood='不会回应外界',
            energy_pace=3,
            mood_pace=2,
            started_at=sleep_at,
            expected_until=end,
            ended_at=end,
        )
        timeline = module.ActivityTimeline(db)

        whole = timeline.integrate_between(start, end)
        awake = timeline.integrate_between(start, sleep_at)
        asleep = timeline.integrate_between(sleep_at, end)

        assert awake.energy_delta == pytest.approx(-32.0)
        assert asleep.energy_delta == pytest.approx(32.0)
        assert whole.energy_delta == pytest.approx(0.0)
        assert whole.mood_delta == pytest.approx(awake.mood_delta + asleep.mood_delta)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_short_gap_extends_previous_and_long_gap_is_backfilled_exactly() -> None:
    """★ 短缺口延续上一段；长缺口写入 backfilled 且完全填满。"""

    module = _timeline_module()

    short_db = _database()
    try:
        now = _timestamp('2032-07-15', 12)
        previous_id = _insert_activity(
            short_db,
            kind='awake',
            doing='写大纲',
            mood='写久了开始烦',
            energy_pace=-2,
            mood_pace=-1,
            started_at=now - 2 * HOUR_MS,
            expected_until=now - 30 * MINUTE_MS,
            ended_at=None,
        )

        async def short_decider(_activity: Any, _now: int, gap_ms: int) -> Any:
            assert gap_ms == 30 * MINUTE_MS
            return module.ActivityTransition(next_activity=_draft(module, doing='去倒杯水'))

        timeline = module.ActivityTimeline(short_db, decider=short_decider)
        timeline.current(now)
        await _allow_background_task()
        previous = short_db.execute(
            'SELECT ended_at FROM activities WHERE id = ?', (previous_id,)
        ).fetchone()
        next_row = short_db.execute(
            'SELECT started_at, source FROM activities ORDER BY id DESC LIMIT 1'
        ).fetchone()
        assert previous is not None and previous[0] == now
        assert next_row is not None and tuple(next_row) == (now, 'decided')
        assert short_db.execute(
            "SELECT COUNT(*) FROM activities WHERE source = 'backfilled'"
        ).fetchone()[0] == 0
    finally:
        short_db.close()

    long_db = _database()
    try:
        now = _timestamp('2032-07-16', 9)
        gap_start = now - 10 * HOUR_MS
        previous_id = _insert_activity(
            long_db,
            kind='awake',
            doing='写大纲',
            mood='已经有点写不动',
            energy_pace=-2,
            mood_pace=-1,
            started_at=gap_start - HOUR_MS,
            expected_until=gap_start,
            ended_at=None,
        )

        async def long_decider(_activity: Any, _now: int, gap_ms: int) -> Any:
            assert gap_ms == 10 * HOUR_MS
            return module.ActivityTransition(
                backfilled=(
                    _draft(module, kind='sleep', doing='睡了一觉', energy_pace=3, minutes=480),
                    _draft(module, kind='awake', doing='醒来后发了会儿呆', minutes=120),
                ),
                next_activity=_draft(module, doing='起床找点吃的'),
            )

        timeline = module.ActivityTimeline(long_db, decider=long_decider)
        timeline.current(now)
        await _allow_background_task()
        rows = long_db.execute(
            '''SELECT started_at, ended_at, source FROM activities
               WHERE id > ? ORDER BY id''',
            (previous_id,),
        ).fetchall()
        assert rows
        assert rows[0]['started_at'] == gap_start
        assert rows[-2]['ended_at'] == now
        assert rows[-1]['started_at'] == now
        assert all(row['source'] == 'backfilled' for row in rows[:-1])
        assert rows[-1]['source'] == 'decided'
        timeline.assert_invariants()
    finally:
        long_db.close()


def test_short_gap_decision_explicitly_continues_or_switches() -> None:
    """★ 短缺口必须明确选择延续当前活动或切换到新活动。"""

    module = _timeline_module()
    continued = module.parse_activity_decision(
        '{"decision":"continue","minutes":45}',
        intention_count=3,
        require_backfill=False,
    )
    assert continued is not None
    assert continued.continuation_minutes == 45
    assert continued.next_activity is None

    switched = module.parse_activity_decision(
        json.dumps({
            'decision': 'switch',
            'activity': {
                'kind': 'awake',
                'doing': '去阳台晾衣服',
                'mood': '换件事做以后轻松了一点',
                'energyPace': -1,
                'moodPace': 1,
                'minutes': 30,
                'advances': None,
            },
        }, ensure_ascii=False),
        intention_count=3,
        require_backfill=False,
    )
    assert switched is not None
    assert switched.next_activity is not None
    assert switched.next_activity.doing == '去阳台晾衣服'
    assert switched.continuation_minutes is None

    legacy_direct_activity = json.dumps({
        'kind': 'awake',
        'doing': '去阳台晾衣服',
        'mood': '换件事做以后轻松了一点',
        'energyPace': -1,
        'moodPace': 1,
        'minutes': 30,
        'advances': None,
    }, ensure_ascii=False)
    assert module.parse_activity_decision(
        legacy_direct_activity,
        intention_count=3,
        require_backfill=False,
    ) is None
    assert module.parse_activity_decision(
        '{"decision":"continue","minutes":45}',
        intention_count=3,
        require_backfill=True,
    ) is None


@pytest.mark.asyncio
async def test_continuing_activity_extends_same_row_without_creating_microstep() -> None:
    """★ 同一核心对象的自然阶段变化只延长原活动，不新增时间线行。"""

    module = _timeline_module()
    db = _database()
    try:
        now = _timestamp('2032-07-15', 15)
        activity_id = _insert_activity(
            db,
            kind='awake',
            doing='在厨房做甜品',
            mood='专心看火候，但被问到时仍会回应',
            energy_pace=-1,
            mood_pace=1,
            started_at=now - HOUR_MS,
            expected_until=now,
            ended_at=None,
        )

        async def continue_decider(_activity: Any, _now: int, gap_ms: int) -> Any:
            assert gap_ms == 0
            return module.ActivityTransition(continuation_minutes=45)

        timeline = module.ActivityTimeline(db, decider=continue_decider)
        timeline.current(now)
        await _allow_background_task()
        rows = db.execute(
            'SELECT id, doing, expected_until, ended_at FROM activities ORDER BY id'
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]['id'] == activity_id
        assert rows[0]['doing'] == '在厨房做甜品'
        assert rows[0]['expected_until'] == now + 45 * MINUTE_MS
        assert rows[0]['ended_at'] is None
        timeline.assert_invariants()
    finally:
        db.close()


def test_activity_prompt_uses_human_scale_episodes_and_continuation() -> None:
    """★ 提示词按人的活动对象决策，并给出延续当前活动的合法表达。"""

    module = _timeline_module()
    now = _timestamp('2032-07-15', 15)
    current = module.Activity(
        id=1,
        kind='awake',
        doing='在厨房做甜品',
        mood='专心看火候，但被问到时仍会回应',
        energy_pace=-1,
        mood_pace=1,
        advances=2,
        started_at=now - HOUR_MS,
        expected_until=now,
        ended_at=None,
        source='decided',
    )
    context = module.ActivityDecisionContext(
        character_name='测试角色',
        character_personality='喜欢自己安排一天。',
        persona='精力 60，心情 55。',
        sleep_history='昨晚睡了八小时',
        intentions='1. 看书\n2. 做甜品',
        intention_count=2,
        rough_rhythm='晚上想早点休息',
        recent_activities='吃午饭 → 在厨房做甜品',
        interaction='还没有互动记录',
    )
    prompt = module.build_activity_prompt(current, now, 0, context)

    assert '先判断是继续当前活动，还是切换核心对象' in prompt
    assert '{"decision":"continue","minutes":45}' in prompt
    assert '{"decision":"switch","activity":' in prompt
    assert '挑教程、备料、烘焙和品尝仍是一次“做甜品”' in prompt
    assert '写代码、调试和测试仍是一次“写脚本”' in prompt


@pytest.mark.asyncio
async def test_current_returns_synchronously_when_decision_fails() -> None:
    """★ 决策失败不阻塞、不抛给调用方，并延长当前活动。"""

    module = _timeline_module()
    db = _database()
    try:
        now = _timestamp('2032-07-15', 12)
        activity_id = _insert_activity(
            db,
            kind='awake',
            doing='写大纲',
            mood='已经有点写不动',
            energy_pace=-2,
            mood_pace=-1,
            started_at=now - HOUR_MS,
            expected_until=now,
            ended_at=None,
        )

        async def failing_decider(_activity: Any, _now: int, _gap_ms: int) -> Any:
            raise RuntimeError('注入的活动决策失败')

        timeline = module.ActivityTimeline(db, decider=failing_decider)
        started = perf_counter()
        current = timeline.current(now)
        elapsed = perf_counter() - started

        assert current.id == activity_id
        assert elapsed < 0.05
        await _allow_background_task()
        row = db.execute(
            'SELECT expected_until, ended_at FROM activities WHERE id = ?',
            (activity_id,),
        ).fetchone()
        assert row is not None
        assert row['expected_until'] > now
        assert row['ended_at'] is None
    finally:
        db.close()


def test_all_asleep_consumers_share_sleep_kind_and_rest_is_awake() -> None:
    """★ 八个消费点共用活动来源；rest 在门控、预算、状态和行动事实中均非睡着。"""

    module = _timeline_module()
    db = _database()
    try:
        now = _timestamp('2032-07-15', 14)
        _insert_activity(
            db,
            kind='rest',
            doing='躺着闭目养神',
            mood='放松下来但仍会回应',
            energy_pace=2,
            mood_pace=1,
            started_at=now,
            expected_until=now + HOUR_MS,
            ended_at=None,
        )
        timeline = module.ActivityTimeline(db)
        controller_type = getattr(import_module('src.core.awareness.sleep'), 'SleepStateController')
        sleep = controller_type(timeline=timeline).current(now)
        assert sleep.asleep is False

        gate = decide_disposition(_gate_request(
            'group',
            name_mentioned=True,
            asleep=sleep.asleep,
        ))
        assert gate.disposition != 'drop' or gate.reason_codes != ('asleep',)

        budget = decide(
            ProactiveState(day_key='2032-07-15', used=0, last_at=0, ignored=0),
            InterruptContext(
                now=now,
                silent=False,
                asleep=sleep.asleep,
                visible=True,
                priority='normal',
            ),
        )
        assert budget.reason != 'asleep'
        assert status_label(
            PersonaState(intimacy=50, energy=50, mood=50, updated_at=now),
            asleep=sleep.asleep,
            just_woke=sleep.just_woke,
            resting=True,
        ) == '休息中'
        assert GateInputFacts(
            stream_kind='group',
            mentioned_me=False,
            name_mentioned=True,
            must_reply=False,
            asleep=sleep.asleep,
            rate_limited=False,
            recent_bot_replies=0,
            candidate_message_ids=(1,),
            selectable_message_ids=(1,),
        ).to_dict()['asleep'] is False

        consumers = {
            'src/core/agent/conversation_gate.py': 'request.asleep',
            'src/core/awareness/budget.py': 'ctx.asleep',
            'src/core/api/http.py': 'current_sleep().asleep',
            'src/core/services/chat.py': 'current_sleep().asleep',
            'src/core/agent/action_protocol.py': "'asleep': self.asleep",
        }
        for relative, marker in consumers.items():
            source = (PROJECT_ROOT / relative).read_text(encoding='utf-8')
            assert marker in source, f'{relative} 没有读取统一 asleep 事实'
            assert "kind == 'rest'" not in source
    finally:
        db.close()


@pytest.mark.parametrize(
    ('gate_request', 'expected'),
    (
        (_gate_request('direct', asleep=True), 'force'),
        (_gate_request(
            'group',
            mentioned_me=True,
            asleep=True,
            at_mention_must_reply=True,
        ), 'force'),
        (_gate_request('group', name_mentioned=True, asleep=True), 'drop'),
        (_gate_request('group', poked_me=True, asleep=True), 'drop'),
    ),
)
def test_sleeping_gate_priority_is_unchanged(gate_request: GateRequest, expected: str) -> None:
    """★ 私聊/@/名字/戳一戳在睡着时仍保持既定优先级。"""

    # 先要求新时间线来源存在，再用同一 asleep 事实复核旧优先级；这样改动前证据
    # 不会把「旧概率状态机碰巧满足表格」误当成新结构已经接通。
    _timeline_module()
    assert decide_disposition(gate_request).disposition == expected


@pytest.mark.parametrize(
    'gate_request',
    (
        _gate_request('direct', asleep=True),
        _gate_request(
            'group',
            mentioned_me=True,
            asleep=True,
            at_mention_must_reply=True,
        ),
    ),
    ids=('private', 'group-at'),
)
def test_force_inbound_interrupts_sleep_immediately(gate_request: GateRequest) -> None:
    """★ 私聊和群协议 @ 两条 force 路径都会结束 sleep 并标 interrupted。"""

    module = _timeline_module()
    db = _database()
    try:
        now = _timestamp('2032-07-15', 3)
        sleep_id = _insert_activity(
            db,
            kind='sleep',
            doing='睡觉',
            mood='睡着后不会回应',
            energy_pace=3,
            mood_pace=1,
            started_at=now - 4 * HOUR_MS,
            expected_until=now + 4 * HOUR_MS,
            ended_at=None,
        )
        timeline = module.ActivityTimeline(db)
        controller_type = getattr(import_module('src.core.awareness.sleep'), 'SleepStateController')
        controller = controller_type(timeline=timeline)
        assert controller.current(now).asleep is True
        assert decide_disposition(gate_request).disposition == 'force'

        state = controller.wake(now)

        assert state.asleep is False
        row = db.execute(
            'SELECT ended_at, source FROM activities WHERE id = ?', (sleep_id,)
        ).fetchone()
        assert row is not None and tuple(row) == (now, 'interrupted')
        assert timeline.current(now).kind == 'awake'
    finally:
        db.close()


@pytest.mark.parametrize('forbidden', ('23:00', '11 点', '11 点半'))
def test_plan_rejects_clock_times_but_activity_doing_accepts_them(forbidden: str) -> None:
    """★ 规划层硬拦钟点；一次具体活动的 doing 不受影响。"""

    raw = {
        'date': '2032-07-15',
        'theme': '今天主要把小论文收尾',
        'intentions': [
            {'what': '把结尾两段理顺', 'carriedDays': 0},
            {'what': '整理书架上散着的书', 'carriedDays': 0},
            {'what': '补完落下的两集番', 'carriedDays': 0},
        ],
        'roughRhythm': '今天想早点睡',
    }
    rough = dict(raw, roughRhythm=f'{forbidden} 睡')
    assert schedule_plan.parse_day_plan(
        json.dumps(rough, ensure_ascii=False),
        raw['date'],
        ScheduleConfig(),
    ) is None
    intention = json.loads(json.dumps(raw, ensure_ascii=False))
    intention['intentions'][0]['what'] = f'{forbidden} 去交作业'
    assert schedule_plan.parse_day_plan(
        json.dumps(intention, ensure_ascii=False),
        raw['date'],
        ScheduleConfig(),
    ) is None

    module = _timeline_module()
    parsed = module.parse_activity_decision(
        json.dumps({
            'decision': 'switch',
            'activity': {
                'kind': 'awake',
                'doing': f'{forbidden} 去交作业',
                'mood': '赶时间所以回话会短一点',
                'energyPace': -1,
                'moodPace': 0,
                'minutes': 30,
                'advances': 1,
            },
        }, ensure_ascii=False),
        intention_count=3,
        require_backfill=False,
    )
    assert parsed is not None
    assert parsed.next_activity is not None
    assert forbidden in parsed.next_activity.doing


@pytest.mark.asyncio
async def test_only_unadvanced_intentions_roll_into_next_prompt() -> None:
    """★ 未推进意向 carriedDays 加一进入次日输入；已推进意向不滚。"""

    module = _timeline_module()
    db = _database()
    store = _PlanStore()
    date = '2032-07-15'
    tomorrow = '2032-07-16'
    store.values[f'day_plan:{date}'] = {
        'date': date,
        'theme': '今天主要处理三件拖着的事',
        'intentions': [
            {'what': '把第三章大纲细化完', 'carriedDays': 2},
            {'what': '收拾堆在椅子上的衣服', 'carriedDays': 0},
            {'what': '补完落下的两集番', 'carriedDays': 1},
        ],
        'roughRhythm': '今天想早点睡',
    }
    start = _timestamp(date, 8)
    _insert_activity(
        db,
        kind='awake',
        doing='收拾椅子上的衣服',
        mood='做完以后轻松了一点',
        energy_pace=-1,
        mood_pace=1,
        advances=2,
        started_at=start,
        expected_until=start + HOUR_MS,
        ended_at=start + HOUR_MS,
    )
    captured: list[str] = []

    class _Generator:
        async def generate(self, prompt: str) -> str:
            captured.append(prompt)
            return json.dumps({
                'date': tomorrow,
                'theme': '今天主要决定欠着的事还要不要做',
                'intentions': [
                    {'what': '把第三章大纲细化完', 'carriedDays': 3},
                    {'what': '补完落下的两集番', 'carriedDays': 2},
                    {'what': '把桌面上散着的东西归位', 'carriedDays': 0},
                ],
                'roughRhythm': '今天顺着状态来',
            }, ensure_ascii=False)

    try:
        service = schedule_plan.DayPlanService(
            db=db,
            store=store,
            persona_state=lambda: PersonaState(50, 60, 50, start),
            interaction_density=lambda _now: '最近偶尔说话',
            anniversary_at=lambda: 0,
            last_interaction_at=lambda: None,
            character_name='测试角色',
            character_personality='按自己的节奏生活',
            generator=_Generator(),
            activity_generator=None,
            schedule_config=ScheduleConfig(),
        )
        await service.ensure(_timestamp(tomorrow, 0, 1))
        assert captured
        prompt = captured[0]
        assert '把第三章大纲细化完（已经滚了 3 天）' in prompt
        assert '补完落下的两集番（已经滚了 2 天）' in prompt
        assert '收拾堆在椅子上的衣服' not in prompt
        assert module.ActivityTimeline(db).advanced_intention_indexes(date) == {2}
    finally:
        db.close()


def test_legacy_day_plan_is_not_read_as_fallback_or_activity() -> None:
    """★ 旧时刻表读到即失效，绝不兼容成新计划或活动事实。"""

    db = _database()
    store = _PlanStore()
    date = '2032-07-15'
    store.values[f'day_plan:{date}'] = {
        'date': date,
        'slots': [{'from': '08:00', 'doing': '写大纲', 'mood': '专注'}],
        'bedtimeHint': '23:00',
        'wakeHint': '07:00',
        'theme': '今天继续写大纲',
        'carryOver': '继续写大纲',
    }
    try:
        service = schedule_plan.DayPlanService(
            db=db,
            store=store,
            persona_state=lambda: PersonaState(50, 60, 50, 0),
            interaction_density=lambda _now: '没有记录',
            anniversary_at=lambda: 0,
            last_interaction_at=lambda: None,
            character_name='测试角色',
            character_personality='按自己的节奏生活',
            generator=None,
            activity_generator=None,
            schedule_config=ScheduleConfig(),
        )
        assert service._read(date) is None
        assert db.execute('SELECT COUNT(*) FROM activities').fetchone()[0] == 0
    finally:
        db.close()


def test_v13_to_v14_keeps_101_legacy_plans_and_starts_empty_timeline(tmp_path: Path) -> None:
    """★ v14 保留 101 份旧 day_plan 元数据，但不读、不伪造成 activities。"""

    path = tmp_path / 'memory.db'
    db = sqlite3.connect(str(path))
    try:
        db.executescript(db_schema.DDL)
        db.executescript(db_schema.SEED)
        db.execute('DROP INDEX IF EXISTS idx_activities_time')
        db.execute('DROP TABLE IF EXISTS activities')
        for index in range(101):
            date = datetime.fromordinal(datetime(2032, 1, 1).toordinal() + index).date().isoformat()
            value = json.dumps({
                'date': date,
                'slots': [{'from': '08:00', 'doing': '写大纲', 'mood': '专注'}],
                'bedtimeHint': '23:00',
                'wakeHint': '07:00',
                'theme': '旧计划',
                'carryOver': '继续写大纲',
            }, ensure_ascii=False)
            db.execute('INSERT INTO meta (key, value) VALUES (?, ?)', (f'day_plan:{date}', value))
        db.execute('PRAGMA user_version = 13')
        db.commit()
        before = db.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'day_plan:%' ORDER BY key"
        ).fetchall()

        run_migrations(db, path)

        after = db.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'day_plan:%' ORDER BY key"
        ).fetchall()
        # 断言链条跑到了头，而不是把头版本号写死：本用例验的是这一步迁移
        # 保住了数据，后面每加一步迁移都要来改一次数字才是错的。
        assert db.execute('PRAGMA user_version').fetchone() == (
            migration_manager.CURRENT_VERSION,
        )
        assert before == after
        assert len(after) == 101
        assert db.execute('SELECT COUNT(*) FROM activities').fetchone() == (0,)
    finally:
        db.close()

    backups = list((tmp_path / 'backups').glob('memory.v13.*.db'))
    assert len(backups) == 1
    backup = sqlite3.connect(str(backups[0]))
    try:
        assert backup.execute('PRAGMA user_version').fetchone() == (13,)
        assert backup.execute(
            "SELECT COUNT(*) FROM meta WHERE key LIKE 'day_plan:%'"
        ).fetchone() == (101,)
        assert backup.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='activities'"
        ).fetchone() is None
    finally:
        backup.close()
