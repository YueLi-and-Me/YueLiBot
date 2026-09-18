"""连续睡眠上限与入眠过近闸门的守护用例。

连续睡眠上限管「一次连续睡眠能申请多长」：continue 与 switch→sleep 两条续睡路径
都按整条首尾相接的 sleep 链判定——未达上限而请求越界时截到「上限 − 链长」，已达
上限仍要求续睡时第一次拒判即由系统收场：同一事务、同一 now 内结束 sleep 段并写入
零时长中性清醒占位段，紧接着以同一 now 对占位段再做一次决策。入眠过近闸门管
「醒后多快能再睡」：距上一条已结束 sleep 链末端不足阈值时不允许新起 sleep 链——
短缺口拒判、不写任何行、走既有续期；长缺口保留补叙、末尾写占位段、不抛。两道
闸门只作用于 sleep，且只在精力系统开启时生效；awake／rest 的提示词与写入行为
逐字不变。闸门是局部行为约束，链上限不是恢复上限：截断前与续期期间照样按 sleep
速率入账。

本文件锁这些行为，全部不调用模型：

- 两条续睡路径各自的截断与收场，以及收场的不留洞、不提前提交；
- 过近拒判的两种形态、外部唤醒后的阈值边界、换新实例（模拟重启）后仍生效；
- 补叙的链长计入与「补叙段本身不判不缩短」；
- 三处提示词新文案与「历史不全」声明的双向断言；
- 决策上下文改用调用方手里的当前段，瞬时读库失败不再误报时间线损坏。

依赖 ``src.core.schedule.timeline``、``src.core.schedule.plan`` 与
``src.core.db.schema``。
"""

from __future__ import annotations

import asyncio
import sqlite3

from dataclasses import dataclass
from structlog.testing import capture_logs
from typing import Any

import pytest

from src.core.db import schema as db_schema
from src.core.persona.state import PersonaState
from src.core.schedule import plan as schedule_plan
from src.core.schedule import timeline as timeline_module
from src.core.schedule.plan import DayPlanService
from src.core.config.schema import ScheduleConfig
from src.core.schedule.timeline import (
    Activity,
    ActivityDecisionContext,
    ActivityDraft,
    ActivityTimeline,
    ActivityTransition,
)

MINUTE_MS = 60_000
# 用整点毫秒做基准，链长与间隔断言全部按分钟精确计算，不经时区换算。
T0 = 1_800_000_000_000
# 三条带事实告警的事件名。用例断言事件名与字段，不比对整句排版。
TRUNCATE_EVENT = '续睡时长按连续睡眠上限截断'
K1_EVENT = '连续睡眠已达上限，拒判续睡并结束该链'
REJECT_EVENT = '距上次睡醒不足阈值，拒判入睡'
DECISION_FAILURE_EVENT = '活动决策失败，沿用当前活动并延后重试'

_KIND_PACE = {'awake': 0, 'rest': 1, 'sleep': 2}


def _sleep_limit() -> int:
    """连续睡眠上限（分钟）；从被测模块读，不在用例里写死。"""

    return timeline_module._CONTINUOUS_SLEEP_LIMIT_MINUTES


def _reentry_threshold() -> int:
    """入眠过近阈值（分钟）；从被测模块读，不在用例里写死。"""

    return timeline_module._SLEEP_REENTRY_MIN_MINUTES


def _database() -> sqlite3.Connection:
    """建一个只含当前 DDL 的内存库，不依赖迁移后的真实数据。"""

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript(db_schema.DDL)
    db.executescript(db_schema.SEED)
    db.commit()
    return db


def _insert_segment(
    timeline: ActivityTimeline,
    kind: str,
    started_at: int,
    ended_at: int | None,
    expected_until: int,
    *,
    doing: str = '闭着眼睛躺着',
) -> int:
    """按精确起止写入一段活动；显式传入 expected_until，minutes 不参与段长。"""

    duration_minutes = max(1, ((ended_at or expected_until) - started_at) // MINUTE_MS)
    return timeline._insert_draft(
        ActivityDraft(
            kind=kind,
            doing=doing,
            mood='安静，但被问到仍会回应',
            energy_pace=_KIND_PACE[kind],
            mood_pace=0,
            minutes=int(duration_minutes),
        ),
        started_at=started_at,
        ended_at=ended_at,
        source='decided',
        expected_until=expected_until,
    )


def _sleep_draft(minutes: int) -> ActivityDraft:
    """构造一份 sleep 切换草案。"""

    return ActivityDraft(
        kind='sleep',
        doing='缩回被子里接着睡',
        mood='困意还很重',
        energy_pace=3,
        mood_pace=0,
        minutes=minutes,
    )


def _awake_draft(minutes: int) -> ActivityDraft:
    """构造一份 awake 切换草案。"""

    return ActivityDraft(
        kind='awake',
        doing='起床洗漱收拾一下',
        mood='慢慢清醒过来',
        energy_pace=0,
        mood_pace=0,
        minutes=minutes,
    )


def _open_activity(db: sqlite3.Connection) -> Activity:
    """读回唯一进行中的活动。"""

    row = db.execute(
        'SELECT * FROM activities WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1'
    ).fetchone()
    assert row is not None, '用例前提是存在一条进行中的活动'
    return timeline_module._activity_from_row(row)


def _until_of(db: sqlite3.Connection, activity_id: int) -> int:
    """读回一段活动的预期结束时刻。"""

    row = db.execute(
        'SELECT expected_until FROM activities WHERE id = ?', (activity_id,),
    ).fetchone()
    assert row is not None, '活动写入后必须能读回'
    return int(row['expected_until'])


def _row_of(db: sqlite3.Connection, activity_id: int) -> sqlite3.Row:
    row = db.execute('SELECT * FROM activities WHERE id = ?', (activity_id,)).fetchone()
    assert row is not None, '活动写入后必须能读回'
    return row


async def _let_background_task_run() -> None:
    """给 ``current()`` 创建的后台决策任务两轮事件循环执行机会。"""

    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _seed_sleep_chain(
    timeline: ActivityTimeline,
    db: sqlite3.Connection,
    first_minutes: int,
    open_elapsed: int,
    *,
    open_expected_ahead: int = 0,
) -> int:
    """构造两段相接的 sleep 链（一段已结束 + 一段进行中），返回进行中段的 id。"""

    _insert_segment(timeline, 'sleep', T0, T0 + first_minutes * MINUTE_MS,
                    T0 + first_minutes * MINUTE_MS)
    open_start = T0 + first_minutes * MINUTE_MS
    open_id = _insert_segment(
        timeline, 'sleep', open_start, None,
        open_start + (open_elapsed + open_expected_ahead) * MINUTE_MS,
    )
    db.commit()
    return open_id


def _context(
    chain_minutes: int | None,
    *,
    reached_start: bool = False,
    last_sleep_end: int | None = None,
    energy_enabled: bool = True,
) -> ActivityDecisionContext:
    """构造一份字段齐全的决策上下文，只让闸门关心的字段随用例变化。"""

    return ActivityDecisionContext(
        character_name='测试角色',
        character_personality='按自己的节奏安排一天。',
        persona='精力 40，心情 55。',
        sleep_history='这一觉已经睡了 2 小时（还没醒）',
        intentions='1. 把第三章大纲收尾',
        intention_count=1,
        rough_rhythm='今天想早点睡',
        recent_activities='吃午饭 → 闭着眼睛躺着',
        interaction='还没有互动记录',
        energy_enabled=energy_enabled,
        current_kind_chain_minutes=chain_minutes,
        current_kind_chain_reached_start=reached_start,
        last_ended_sleep_end=last_sleep_end,
    )


# ---------------------------------------------------------------- 两条续睡路径：截断


def test_continue_on_sleep_chain_is_truncated_to_the_limit() -> None:
    """链未达上限时 continue 续睡被截到「上限 − 链长」，告警带链事实与原始请求。"""

    # 夹具按「上限 540、链 500、请求 120」构造，截断期望 40 分钟按夹具算术写死：
    # 本用例先在引入常量前的旧代码上跑出行为性失败，告警字段再从被测模块读常量。
    first, elapsed, request = 380, 120, 120
    chain = first + elapsed
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        open_id = _seed_sleep_chain(timeline, db, first, elapsed)
        now = T0 + chain * MINUTE_MS

        with capture_logs() as logs:
            timeline._apply_transition(
                _open_activity(db),
                ActivityTransition(continuation_minutes=request),
                now,
                0,
            )

        assert _until_of(db, open_id) == now + 40 * MINUTE_MS, (
            '续睡必须截到「上限 − 链长」：链 500、上限 540，应写 40 分钟'
        )
        limit = _sleep_limit()
        assert chain < limit < chain + request, '夹具前提是请求越过上限但链未达上限'
        truncated = [entry for entry in logs if entry.get('event') == TRUNCATE_EVENT]
        assert len(truncated) == 1
        assert truncated[0]['raw'] == request
        assert truncated[0]['clamped'] == limit - chain
        assert truncated[0]['chain_minutes'] == chain
        assert truncated[0]['limit'] == limit
        timeline.assert_invariants()
    finally:
        db.close()


def test_switch_to_sleep_on_sleep_chain_is_truncated_to_the_limit() -> None:
    """链未达上限时 switch→sleep 的新段时长同样截到「上限 − 链长」。"""

    # 夹具按「上限 540、链 500、请求 300」构造，截断期望 40 分钟按夹具算术写死。
    first, elapsed, request = 380, 120, 300
    chain = first + elapsed
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _seed_sleep_chain(timeline, db, first, elapsed)
        now = T0 + chain * MINUTE_MS

        with capture_logs() as logs:
            timeline._apply_transition(
                _open_activity(db),
                ActivityTransition(next_activity=_sleep_draft(request)),
                now,
                0,
            )

        current = _open_activity(db)
        assert current.kind == 'sleep'
        assert current.started_at == now, '前一段在 now 结束，新段从 now 开始'
        assert current.expected_until == now + 40 * MINUTE_MS, (
            '新 sleep 段必须截到「上限 − 链长」：链 500、上限 540，应写 40 分钟'
        )
        limit = _sleep_limit()
        assert chain < limit < chain + request, '夹具前提是请求越过上限但链未达上限'
        truncated = [entry for entry in logs if entry.get('event') == TRUNCATE_EVENT]
        assert len(truncated) == 1
        assert truncated[0]['raw'] == request
        assert truncated[0]['clamped'] == limit - chain
        assert truncated[0]['chain_minutes'] == chain
        timeline.assert_invariants()
    finally:
        db.close()


def test_continue_truncation_writes_less_than_ten_minutes_when_budget_is_small() -> None:
    """「上限 − 链长」不足 10 分钟也照写：这一段停在链上限上，下一次边界收场。"""

    limit = _sleep_limit()
    first, elapsed, request = 480, 55, 120
    chain = first + elapsed
    assert 0 < limit - chain < 10, '夹具前提是剩余额度不足单次决策下限'
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        open_id = _seed_sleep_chain(timeline, db, first, elapsed)
        now = T0 + chain * MINUTE_MS

        timeline._apply_transition(
            _open_activity(db),
            ActivityTransition(continuation_minutes=request),
            now,
            0,
        )

        assert _until_of(db, open_id) == now + (limit - chain) * MINUTE_MS
        timeline.assert_invariants()
    finally:
        db.close()


# ---------------------------------------------------------------- 两条续睡路径：K=1 收场


@pytest.mark.asyncio
async def test_continue_on_sleep_chain_at_limit_triggers_k1_and_followup() -> None:
    """链已达上限仍 continue：同一事务同一 now 结束睡眠、写零时长占位段并立即再决策。

    收场后时间线不留洞：sleep 段 ended_at=now，占位段 started=expected=ended=now
    （零时长、source='decided'），紧接着的决策以同一个 now 作用于占位段。
    """

    # 夹具链长 560 ≥ 上限 540：旧代码没有收场行为，本用例先跑出行为性失败；
    # 链上限数值断言在下方从被测模块读常量。
    first, elapsed = 440, 120
    chain = first + elapsed
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        sleep_id = _seed_sleep_chain(timeline, db, first, elapsed)
        now = T0 + chain * MINUTE_MS
        calls: list[tuple[int, int]] = []

        async def decider(activity: Activity, at: int, _gap_ms: int) -> ActivityTransition:
            calls.append((activity.id, at))
            if len(calls) == 1:
                return ActivityTransition(continuation_minutes=120)
            return ActivityTransition(next_activity=_awake_draft(30))

        timeline.set_decider(decider)
        with capture_logs() as logs:
            timeline.current(now)
            await _let_background_task_run()

        assert [at for _, at in calls] == [now, now], '两次决策必须发生在同一个 now'
        placeholder_id = calls[1][0]
        assert placeholder_id != sleep_id, '第二次决策必须作用于新写的占位段'

        sleep_row = _row_of(db, sleep_id)
        placeholder = _row_of(db, placeholder_id)
        current = _open_activity(db)
        assert sleep_row['ended_at'] == now, 'sleep 段在同一个 now 被结束'
        assert placeholder['kind'] == 'awake'
        assert placeholder['started_at'] == now
        assert placeholder['expected_until'] == now
        assert placeholder['ended_at'] == now, '占位段是零时长段，紧接着被第二次决策结束'
        assert placeholder['source'] == 'decided'
        assert current.kind == 'awake' and current.started_at == now

        limit = _sleep_limit()
        k1 = [entry for entry in logs if entry.get('event') == K1_EVENT]
        assert len(k1) == 1
        assert k1[0]['chain_minutes'] == chain
        assert k1[0]['limit'] == limit
        assert k1[0]['raw'] == 120
        timeline.assert_invariants()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_switch_to_sleep_on_chain_at_limit_triggers_k1_and_followup() -> None:
    """链已达上限仍 switch→sleep：同样第一次拒判即收场，新 sleep 段不写入。"""

    # 夹具链长 560 ≥ 上限 540：旧代码没有收场行为，本用例先跑出行为性失败。
    first, elapsed = 440, 120
    chain = first + elapsed
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        sleep_id = _seed_sleep_chain(timeline, db, first, elapsed)
        now = T0 + chain * MINUTE_MS
        calls: list[tuple[int, int]] = []

        async def decider(activity: Activity, at: int, _gap_ms: int) -> ActivityTransition:
            calls.append((activity.id, at))
            if len(calls) == 1:
                return ActivityTransition(next_activity=_sleep_draft(300))
            return ActivityTransition(next_activity=_awake_draft(30))

        timeline.set_decider(decider)
        with capture_logs() as logs:
            timeline.current(now)
            await _let_background_task_run()

        assert [at for _, at in calls] == [now, now]
        sleep_rows = db.execute(
            "SELECT COUNT(*) FROM activities WHERE kind = 'sleep'"
        ).fetchone()[0]
        assert sleep_rows == 2, '被拒的 switch→sleep 不得再写入新 sleep 段'
        current = _open_activity(db)
        assert current.kind == 'awake' and current.started_at == now
        assert _row_of(db, sleep_id)['ended_at'] == now
        assert len([entry for entry in logs if entry.get('event') == K1_EVENT]) == 1
        timeline.assert_invariants()
    finally:
        db.close()


def test_k1_placeholder_failure_rolls_back_the_sleep_ending() -> None:
    """占位段写入失败时 sleep 段的结束一并回滚：收场是一个事务，不提前提交。

    在 ``_insert_draft`` 写占位段处注入异常；若收场改回「先结束 sleep、占位段留给
    下一次」或在内层自行提交事务，这条用例立即变红。
    """

    # 夹具链长 560 ≥ 上限 540：旧代码没有收场行为，异常注入点永远不会触发，
    # pytest.raises 收不到异常即行为性失败。
    first, elapsed = 440, 120
    chain = first + elapsed
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        sleep_id = _seed_sleep_chain(timeline, db, first, elapsed)
        now = T0 + chain * MINUTE_MS

        original_insert = timeline._insert_draft

        def exploding_insert(draft: ActivityDraft, **kwargs: Any) -> int:
            if '刚停下来' in draft.doing:
                raise sqlite3.OperationalError('注入的占位段写入失败')
            return original_insert(draft, **kwargs)

        timeline._insert_draft = exploding_insert  # type: ignore[method-assign]

        with pytest.raises(sqlite3.OperationalError, match='注入的占位段写入失败'):
            timeline._apply_transition(
                _open_activity(db),
                ActivityTransition(continuation_minutes=120),
                now,
                0,
            )

        rows = db.execute('SELECT id, ended_at FROM activities ORDER BY id').fetchall()
        assert len(rows) == 2, '占位段写入失败不得留下任何新行'
        assert _row_of(db, sleep_id)['ended_at'] is None, 'sleep 段的结束必须一并回滚'
    finally:
        db.close()


# ---------------------------------------------------------------- 过近拒判


def test_reentry_within_threshold_is_rejected_without_any_write() -> None:
    """短缺口过近拒判：抛 ValueError 带事实，不写任何行，告警带链末端、间隔与阈值。"""

    threshold = _reentry_threshold()
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0 - 180 * MINUTE_MS, T0, T0)
        awake_id = _insert_segment(timeline, 'awake', T0, None, T0 + 30 * MINUTE_MS)
        db.commit()
        interval = 20
        assert interval < threshold, '夹具前提是间隔不足阈值'
        now = T0 + interval * MINUTE_MS
        before = db.execute('SELECT COUNT(*) FROM activities').fetchone()[0]

        with capture_logs() as logs, pytest.raises(ValueError) as error:
            timeline._apply_transition(
                _open_activity(db),
                ActivityTransition(next_activity=_sleep_draft(120)),
                now,
                0,
            )

        message = str(error.value)
        assert str(interval) in message
        assert str(threshold) in message
        assert str(T0) in message, 'ValueError 文本必须带上一条 sleep 链末端'
        assert db.execute('SELECT COUNT(*) FROM activities').fetchone()[0] == before, (
            '过近拒判不得写入任何行'
        )
        assert _until_of(db, awake_id) == T0 + 30 * MINUTE_MS, 'expected_until 不得被改写'
        assert _open_activity(db).kind == 'awake'
        rejected = [entry for entry in logs if entry.get('event') == REJECT_EVENT]
        assert len(rejected) == 1
        assert rejected[0]['last_sleep_end'] == T0
        assert rejected[0]['interval_minutes'] == interval
        assert rejected[0]['threshold'] == threshold
    finally:
        db.close()


@pytest.mark.asyncio
async def test_reentry_retries_until_threshold_passes_naturally() -> None:
    """过近拒判走既有续期：每次拒判只延后重试，间隔越过阈值后自然放行，有界。"""

    threshold = _reentry_threshold()
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0 - 180 * MINUTE_MS, T0, T0)
        _insert_segment(timeline, 'awake', T0, None, T0 + 10 * MINUTE_MS)
        db.commit()
        calls: list[int] = []

        async def sleepy(_activity: Activity, at: int, _gap_ms: int) -> ActivityTransition:
            calls.append(at)
            return ActivityTransition(next_activity=_sleep_draft(120))

        timeline.set_decider(sleepy)
        retry = timeline_module.DECISION_RETRY_MS // MINUTE_MS
        with capture_logs():
            timeline.current(T0 + 20 * MINUTE_MS)
            await _let_background_task_run()
            timeline.current(T0 + (20 + retry) * MINUTE_MS)
            await _let_background_task_run()
            assert db.execute(
                "SELECT COUNT(*) FROM activities WHERE kind = 'sleep'"
            ).fetchone()[0] == 1, '两次拒判之间不得写入新 sleep 段'
            timeline.current(T0 + threshold * MINUTE_MS)
            await _let_background_task_run()

        assert calls == [
            T0 + 20 * MINUTE_MS,
            T0 + (20 + retry) * MINUTE_MS,
            T0 + threshold * MINUTE_MS,
        ], '拒判只按续期间隔重试，间隔一够阈值就放行'
        assert _open_activity(db).kind == 'sleep'
        timeline.assert_invariants()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_reentry_long_gap_keeps_backfill_and_writes_placeholder() -> None:
    """长缺口过近拒判：补叙照写不改，末尾写零时长占位段，不抛错、不写 sleep 下一段。

    夹具里持久化的最近一次 sleep 结束在 11 小时前，本不触发过近；是补叙里的
    sleep 段（结束于 now 前 30 分钟）让候选时间线判出「刚醒又睡」——判定用的是
    含补叙的候选时间线，不是只看持久化历史。
    """

    threshold = _reentry_threshold()
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0 - 20 * 60 * MINUTE_MS,
                        T0 - 11 * 60 * MINUTE_MS, T0 - 11 * 60 * MINUTE_MS)
        _insert_segment(timeline, 'awake', T0 - 11 * 60 * MINUTE_MS, None,
                        T0 - 10 * 60 * MINUTE_MS)
        db.commit()
        now = T0
        gap_minutes = 10 * 60
        backfill = (
            ActivityDraft(kind='sleep', doing='缺口里睡了很久', mood='睡得沉',
                          energy_pace=3, mood_pace=0, minutes=gap_minutes - 30),
            ActivityDraft(kind='awake', doing='醒着发了会儿呆', mood='安静',
                          energy_pace=0, mood_pace=0, minutes=30),
        )
        calls: list[tuple[int, int]] = []

        async def decider(activity: Activity, at: int, _gap_ms: int) -> ActivityTransition:
            calls.append((activity.id, at))
            if len(calls) == 1:
                return ActivityTransition(next_activity=_sleep_draft(120), backfilled=backfill)
            return ActivityTransition(next_activity=_awake_draft(30))

        timeline.set_decider(decider)
        with capture_logs() as logs:
            timeline.current(now)
            await _let_background_task_run()

        assert [at for _, at in calls] == [now, now]
        rows = db.execute(
            'SELECT kind, started_at, ended_at, source FROM activities ORDER BY id'
        ).fetchall()
        backfilled = [row for row in rows if row['source'] == 'backfilled']
        assert len(backfilled) == 2, '补叙必须照写，不缩短、不改填、不回滚'
        assert backfilled[0]['kind'] == 'sleep'
        assert (backfilled[0]['ended_at'] - backfilled[0]['started_at']) == (
            gap_minutes - 30
        ) * MINUTE_MS
        assert backfilled[1]['kind'] == 'awake'
        assert backfilled[1]['ended_at'] == now, '补叙收尾刚好铺到 now'
        placeholder = rows[-2]
        assert placeholder['kind'] == 'awake'
        assert placeholder['started_at'] == placeholder['ended_at'] == now, (
            '末尾是零时长占位段，且紧接着的决策以同一 now 把它结束'
        )
        assert rows[-1]['kind'] == 'awake' and rows[-1]['started_at'] == now
        assert db.execute(
            "SELECT COUNT(*) FROM activities WHERE kind = 'sleep' AND started_at >= ?",
            (now,),
        ).fetchone()[0] == 0, '被拒的 sleep 下一段不得写入'
        rejected = [entry for entry in logs if entry.get('event') == REJECT_EVENT]
        assert len(rejected) == 1
        assert rejected[0]['interval_minutes'] == 30
        assert rejected[0]['threshold'] == threshold
        timeline.assert_invariants()
    finally:
        db.close()


def test_reentry_after_external_wake_boundary_at_exactly_threshold() -> None:
    """外部唤醒不受链上限约束，但之后的入睡受过近约束：阈值边界一拒一放。

    换新 ``ActivityTimeline`` 实例（模拟重启）后判定仍然生效：过近只读持久化
    时间线，不读任何进程内状态。
    """

    threshold = _reentry_threshold()
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0 - 300 * MINUTE_MS, None, T0 + 300 * MINUTE_MS)
        db.commit()
        timeline.note_woken(T0)

        with pytest.raises(ValueError):
            timeline._apply_transition(
                _open_activity(db),
                ActivityTransition(next_activity=_sleep_draft(120)),
                T0 + (threshold - 1) * MINUTE_MS,
                0,
            )

        # 换一个全新实例再试一次：同样要拒（持久化判据，不读进程内状态）。
        restarted = ActivityTimeline(db)
        with pytest.raises(ValueError):
            restarted._apply_transition(
                _open_activity(db),
                ActivityTransition(next_activity=_sleep_draft(120)),
                T0 + (threshold - 1) * MINUTE_MS,
                0,
            )

        restarted._apply_transition(
            _open_activity(db),
            ActivityTransition(next_activity=_sleep_draft(120)),
            T0 + threshold * MINUTE_MS,
            0,
        )
        assert _open_activity(db).kind == 'sleep', '恰好到阈值必须放行'
        timeline.assert_invariants()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_k1_followup_sleep_request_is_rejected_by_reentry_rule() -> None:
    """收场后紧接着的决策仍要 sleep：上一条链末端正是 now，按过近拒判并续期。"""

    limit = _sleep_limit()
    first, elapsed = 440, 120
    chain = first + elapsed
    assert chain >= limit, '夹具前提是链已达上限'
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _seed_sleep_chain(timeline, db, first, elapsed)
        now = T0 + chain * MINUTE_MS
        calls: list[tuple[int, int]] = []

        async def decider(activity: Activity, at: int, _gap_ms: int) -> ActivityTransition:
            calls.append((activity.id, at))
            if len(calls) == 1:
                return ActivityTransition(continuation_minutes=120)
            return ActivityTransition(next_activity=_sleep_draft(120))

        timeline.set_decider(decider)
        with capture_logs() as logs:
            timeline.current(now)
            await _let_background_task_run()

        assert [at for _, at in calls] == [now, now]
        placeholder_id = calls[1][0]
        placeholder = _row_of(db, placeholder_id)
        assert placeholder['kind'] == 'awake'
        assert placeholder['ended_at'] is None, '过近拒判不写任何行，占位段保持进行中'
        assert placeholder['expected_until'] == now + timeline_module.DECISION_RETRY_MS, (
            '这一次决策失败走既有续期，不递归'
        )
        assert db.execute(
            "SELECT COUNT(*) FROM activities WHERE kind = 'sleep' AND started_at >= ?",
            (now,),
        ).fetchone()[0] == 0, '被拒的 sleep 下一段不得写入'
        failures = [
            entry for entry in logs if entry.get('event') == DECISION_FAILURE_EVENT
        ]
        assert len(failures) == 1
        assert str(_reentry_threshold()) in str(failures[0]['error'])
        timeline.assert_invariants()
    finally:
        db.close()


# ---------------------------------------------------------------- 补叙


def test_backfill_sleep_tail_counts_toward_the_chain_for_next_sleep() -> None:
    """补叙以 sleep 收尾时，next=sleep 的链长包含补叙段；补叙段本身不判不缩短。"""

    limit = _sleep_limit()
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'awake', T0 - 11 * 60 * MINUTE_MS, None,
                        T0 - 10 * 60 * MINUTE_MS)
        db.commit()
        now = T0
        gap_minutes = 10 * 60
        backfill = (
            ActivityDraft(kind='awake', doing='缺口前段醒着', mood='安静',
                          energy_pace=0, mood_pace=0, minutes=100),
            ActivityDraft(kind='sleep', doing='缺口后段睡着了', mood='睡得沉',
                          energy_pace=3, mood_pace=0, minutes=gap_minutes - 100),
        )
        chain = gap_minutes - 100
        request = 120
        assert chain < limit < chain + request, '夹具前提是补叙链未达上限而请求越界'

        with capture_logs() as logs:
            timeline._apply_transition(
                _open_activity(db),
                ActivityTransition(next_activity=_sleep_draft(request), backfilled=backfill),
                now,
                gap_minutes * MINUTE_MS,
            )

        rows = db.execute(
            'SELECT kind, started_at, ended_at, expected_until, source FROM activities ORDER BY id'
        ).fetchall()
        backfilled = [row for row in rows if row['source'] == 'backfilled']
        assert len(backfilled) == 2
        assert (backfilled[1]['ended_at'] - backfilled[1]['started_at']) == (
            chain * MINUTE_MS
        ), '补叙 sleep 段照模型给出的相对占比铺满，不缩短'
        current = _open_activity(db)
        assert current.kind == 'sleep'
        assert current.expected_until == now + (limit - chain) * MINUTE_MS, (
            'next=sleep 的链长必须把补叙 sleep 尾段算进来'
        )
        truncated = [entry for entry in logs if entry.get('event') == TRUNCATE_EVENT]
        assert len(truncated) == 1
        assert truncated[0]['chain_minutes'] == chain
        assert truncated[0]['raw'] == request
        assert truncated[0]['clamped'] == limit - chain
        timeline.assert_invariants()
    finally:
        db.close()


def test_backfill_segment_beyond_limit_is_written_unchanged() -> None:
    """补叙段自身超过连续睡眠上限也照写不改：它不判、不缩短、不回滚。"""

    limit = _sleep_limit()
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'awake', T0 - 11 * 60 * MINUTE_MS, None,
                        T0 - 10 * 60 * MINUTE_MS)
        db.commit()
        now = T0
        gap_minutes = 10 * 60
        backfill = (
            ActivityDraft(kind='sleep', doing='缺口里一直睡', mood='睡得沉',
                          energy_pace=3, mood_pace=0, minutes=gap_minutes),
        )
        assert gap_minutes > limit, '夹具前提是补叙 sleep 段自身超过上限'

        timeline._apply_transition(
            _open_activity(db),
            ActivityTransition(next_activity=_awake_draft(30), backfilled=backfill),
            now,
            gap_minutes * MINUTE_MS,
        )

        row = db.execute(
            "SELECT started_at, ended_at FROM activities WHERE source = 'backfilled'"
        ).fetchone()
        assert row is not None
        assert (row['ended_at'] - row['started_at']) == gap_minutes * MINUTE_MS, (
            '补叙段超过上限也照写不改'
        )
        timeline.assert_invariants()
    finally:
        db.close()


# ---------------------------------------------------------------- 提示词文案


def _sleep_current(now: int, elapsed: int) -> Activity:
    """构造一段进行中的 sleep 活动。"""

    return Activity(
        id=7,
        kind='sleep',
        doing='抱着被子熟睡',
        mood='睡得安稳',
        energy_pace=3,
        mood_pace=0,
        advances=None,
        started_at=now - elapsed * MINUTE_MS,
        expected_until=now + 60 * MINUTE_MS,
        ended_at=None,
        source='decided',
    )


def _awake_current(now: int, elapsed: int) -> Activity:
    """构造一段进行中的 awake 活动。"""

    return Activity(
        id=7,
        kind='awake',
        doing='窝在书桌前刷视频',
        mood='放松，被问到仍会回应',
        energy_pace=0,
        mood_pace=0,
        advances=None,
        started_at=now - elapsed * MINUTE_MS,
        expected_until=now + 60 * MINUTE_MS,
        ended_at=None,
        source='decided',
    )


ALLOW_SLEEP_TEXT = '允许选择 sleep。按这一段的打算选'
ALLOW_CONTINUE_TEXT = '{"decision":"continue","minutes":45}'


def test_sleep_rule_and_short_gap_rule_show_chain_cap_variants() -> None:
    """双向断言：sleep 链达上限时两处文案换成链上限变体，旧的允许文案退场。"""

    limit = _sleep_limit()
    now = T0 + 600 * MINUTE_MS
    chain = limit + 20
    chain_text = timeline_module._duration_text(chain * MINUTE_MS)
    limit_text = timeline_module._duration_text(limit * MINUTE_MS)
    prompt = timeline_module.build_activity_prompt(
        _sleep_current(now, 120), now, 0, _context(chain),
    )

    assert (
        f'这次睡眠已经连续 {chain_text}，达到连续睡眠上限（{limit_text}），'
        '这一轮不允许选择 sleep。'
    ) in prompt
    assert (
        f'没有长缺口。这次睡眠已经连续 {chain_text}，达到连续睡眠上限（{limit_text}），'
        '必须切换核心对象：'
    ) in prompt
    assert ALLOW_SLEEP_TEXT not in prompt, '链达上限时允许 sleep 的旧文案必须退场'
    assert ALLOW_CONTINUE_TEXT not in prompt, '链达上限时 continue 选项必须退场'
    assert '这一轮不允许延续当前活动' in prompt


def test_sleep_rule_shows_reentry_variant_and_keeps_default_otherwise() -> None:
    """双向断言：距睡醒不足阈值时 sleep_rule 换成过近变体，其余情况维持允许文案。"""

    threshold = _reentry_threshold()
    now = T0 + 600 * MINUTE_MS
    interval = 20
    prompt = timeline_module.build_activity_prompt(
        _awake_current(now, 30), now, 0,
        _context(30, last_sleep_end=now - interval * MINUTE_MS),
    )

    assert (
        f'上次睡醒到现在只有 {interval} 分钟，不到 '
        f'{timeline_module._duration_text(threshold * MINUTE_MS)}，这一轮不允许选择 sleep。'
    ) in prompt
    assert ALLOW_SLEEP_TEXT not in prompt, '过近时允许 sleep 的旧文案必须退场'

    far = timeline_module.build_activity_prompt(
        _awake_current(now, 30), now, 0,
        _context(30, last_sleep_end=now - (threshold + 40) * MINUTE_MS),
    )
    assert ALLOW_SLEEP_TEXT in far, '间隔够远时维持允许文案'
    assert '不允许选择 sleep' not in far

    never_slept = timeline_module.build_activity_prompt(
        _awake_current(now, 30), now, 0, _context(30, last_sleep_end=None),
    )
    assert ALLOW_SLEEP_TEXT in never_slept, '没有睡眠记录时不判过近'


def test_history_marker_only_for_sleep_chain_reaching_record_start() -> None:
    """「更早没有记录」声明只在 sleep 链抵达记录起点时出现；awake 链不加。"""

    now = T0 + 600 * MINUTE_MS
    sleep_prompt = timeline_module.build_activity_prompt(
        _sleep_current(now, 120), now, 0,
        _context(480, reached_start=True),
    )
    assert (
        '算上首尾相接的之前几段，这种 sleep 状态已经连续 8 小时'
        '（更早没有记录，实际可能更长）'
    ) in sleep_prompt

    awake_prompt = timeline_module.build_activity_prompt(
        _awake_current(now, 120), now, 0,
        _context(480, reached_start=True),
    )
    assert '更早没有记录' not in awake_prompt, (
        'awake 链不加声明：冷启动首行必然是 awake，它是真实起点而不是缺失的历史'
    )

    sleep_mid = timeline_module.build_activity_prompt(
        _sleep_current(now, 120), now, 0,
        _context(480, reached_start=False),
    )
    assert '更早没有记录' not in sleep_mid, 'sleep 链未抵达记录起点时不加声明'


def test_prompts_unchanged_when_energy_is_disabled() -> None:
    """精力关闭时现有路径不变：sleep_rule 是关闭文案，短缺口规则不按链变体。"""

    limit = _sleep_limit()
    now = T0 + 600 * MINUTE_MS
    prompt = timeline_module.build_activity_prompt(
        _sleep_current(now, limit + 20), now, 0,
        _context(limit + 20, energy_enabled=False),
    )

    assert '当前精力系统已关闭，不允许选择 sleep' in prompt
    assert '达到连续睡眠上限' not in prompt, '精力关闭时不出现链上限文案'


# ---------------------------------------------------------------- 写入行为回归


def test_gates_do_not_apply_when_energy_is_disabled() -> None:
    """精力关闭时写入行为不变：sleep 链超上限的 continue 照常延长，不收场。"""

    limit = _sleep_limit()
    first, elapsed = 440, 120
    chain = first + elapsed
    assert chain >= limit, '夹具前提是链已达上限'
    db = _database()
    try:
        timeline = ActivityTimeline(db)
        timeline.set_energy_enabled(False)
        open_id = _seed_sleep_chain(timeline, db, first, elapsed)
        now = T0 + chain * MINUTE_MS

        with capture_logs() as logs:
            timeline._apply_transition(
                _open_activity(db),
                ActivityTransition(continuation_minutes=120),
                now,
                0,
            )

        assert _until_of(db, open_id) == now + 120 * MINUTE_MS, (
            '精力关闭时 continue 照常延长，不截断、不收场'
        )
        assert _open_activity(db).kind == 'sleep'
        assert [entry for entry in logs if entry.get('event') in (TRUNCATE_EVENT, K1_EVENT)] == []
        timeline.assert_invariants()
    finally:
        db.close()


# ---------------------------------------------------------------- 决策上下文


@pytest.mark.asyncio
async def test_decide_passes_its_own_current_to_the_context_callback() -> None:
    """决策上下文回调拿到的是 decide() 手里的那一条当前段，不再自行重读。"""

    now = T0 + 30 * MINUTE_MS
    current = Activity(
        id=7,
        kind='awake',
        doing='窝在书桌前刷视频',
        mood='放松',
        energy_pace=0,
        mood_pace=0,
        advances=None,
        started_at=T0,
        expected_until=now,
        ended_at=None,
        source='decided',
    )
    seen: list[tuple[int, int]] = []

    def context(activity: Activity, at: int) -> ActivityDecisionContext:
        seen.append((activity.id, at))
        return _context(None)

    class _Generator:
        async def generate(self, _prompt: str) -> str:
            return '{"decision":"continue","minutes":30}'

    service = timeline_module.ActivityDecisionService(_Generator(), context)
    transition = await service.decide(current, now, 0)

    assert seen == [(7, now)]
    assert transition.continuation_minutes == 30


@dataclass
class _PlanStore:
    """为每日方向提供隔离的 JSON 存储桩。"""

    values: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.values is None:
            self.values = {}

    def read_json(self, key: str, fallback: Any) -> Any:
        return (self.values or {}).get(key, fallback)

    def write_json(self, key: str, value: Any) -> None:
        (self.values or {})[key] = value


def test_activity_decision_context_no_longer_rereads_current() -> None:
    """瞬时读库失败不再让链长函数把 id=0 中性段误报为时间线损坏。

     sabotage 掉 ``timeline.current``（让它返回 id=0 中性段）后，用调用方手里的
    真实当前段组装上下文：链长照常算出，不抛「当前段不在表中」。
    """

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'awake', T0, None, T0 + 60 * MINUTE_MS)
        db.commit()
        now = T0 + 30 * MINUTE_MS
        service = DayPlanService(
            db=db,
            timeline=timeline,
            store=_PlanStore(),
            persona_state=lambda: PersonaState(60, 40, 55, now),
            interaction_density=lambda _now: '最近偶尔说话',
            anniversary_at=lambda: 0,
            last_interaction_at=lambda: None,
            character_name='测试角色',
            character_personality='按自己的节奏生活',
            generator=None,
            activity_generator=None,
            schedule_config=ScheduleConfig(),
        )
        real_current = _open_activity(db)
        timeline.current = timeline_module._neutral_activity  # type: ignore[method-assign]

        context = service.activity_decision_context(real_current, now)

        assert context.current_kind_chain_minutes == 30
        assert context.current_kind_chain_reached_start is True
    finally:
        db.close()


# ---------------------------------------------------------------- 时间线不变量


def test_assert_invariants_rejects_negative_duration_segment() -> None:
    """写入层与链长函数的损坏定义一致：负时长段过不了 assert_invariants。"""

    db = _database()
    try:
        timeline = ActivityTimeline(db)
        _insert_segment(timeline, 'sleep', T0, T0 + 100 * MINUTE_MS, T0 + 100 * MINUTE_MS)
        db.execute(
            """INSERT INTO activities
                 (kind, doing, mood, energy_pace, mood_pace, advances,
                  started_at, expected_until, ended_at, source)
               VALUES ('sleep', '损坏段', '损坏段', 2, 0, NULL, ?, ?, ?, 'decided')""",
            (T0 + 300 * MINUTE_MS, T0 + 200 * MINUTE_MS, T0 + 200 * MINUTE_MS),
        )
        _insert_segment(timeline, 'awake', T0 + 400 * MINUTE_MS, None, T0 + 500 * MINUTE_MS)
        db.commit()

        with pytest.raises(RuntimeError, match='负时长'):
            timeline.assert_invariants()
    finally:
        db.close()
