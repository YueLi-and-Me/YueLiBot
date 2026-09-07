"""锁死「对话不得吞掉休息」：精力的时间结算游标与回合结算互不干扰。

这条回归对应一个真机故障：精力跌到 0 之后再不回升，而当时所有用例都是绿的。
根因是时间结算与回合结算共用 ``persona_bond.updated_at`` 一个游标——
:meth:`Persona.apply_elapsed` 在不足一小时时刻意早退、不推进游标，靠的是「没有别人
动它」，而 ``apply_turn`` 每个回合都把它推到当前时刻，于是累积窗口永远到不了一小时，
两次对话之间的休息被整段丢弃。

既有用例看不见这个缺陷，是因为它们只驱动时间、从不产生对话回合。本文件的两个场景
必须成对存在：只有把「有对话」与「无对话」放在一起比，游标被谁重置才有判据。

依赖 ``src.core.persona.state``。
"""

from __future__ import annotations

import sqlite3

from src.core.persona.state import (
    ENERGY_RATE,
    TURN_ENERGY_COST,
    ElapsedEffect,
    Persona,
)

HOUR_MS = 3_600_000
TEN_MINUTES_MS = 10 * 60_000
# 休息类活动（pace=2）的每小时精力：时间线按 ENERGY_RATE * (pace - 1) 积分。
# 引用常量而不是抄数字——调速率时用例应当跟着走，而不是变成第二份判据。
REST_ENERGY_PER_HOUR = ENERGY_RATE


def _owner_id(db: sqlite3.Connection) -> int:
    """取出 owner 的稳定主键。"""
    row = db.execute("SELECT id FROM persons WHERE kind = 'owner'").fetchone()
    assert row is not None, 'owner 人物不存在，确认迁移已完整执行'
    return int(row[0])


def _reset(db: sqlite3.Connection, person_id: int, energy: float, at: int) -> None:
    """把精力与两个游标都放到同一起点，使两个场景可比。"""
    db.execute(
        'UPDATE persona_self SET energy = ?, mood = 50, updated_at = ?', (energy, at)
    )
    db.execute(
        'UPDATE persona_bond SET updated_at = ? WHERE person_id = ?', (at, person_id)
    )
    db.commit()


def _rest_effect(hours: float) -> ElapsedEffect:
    """构造一段纯休息的积分结果，替代日程时间线。"""
    return ElapsedEffect(energy_delta=REST_ENERGY_PER_HOUR * hours, mood_delta=0.0)


def _simulate(
    persona: Persona,
    person_id: int,
    start: int,
    steps: int,
    *,
    with_turns: bool,
) -> float:
    """按十分钟一步推进，返回结束时的精力。

    :param with_turns: 每一步是否附带一次回合结算，用于区分两个场景。

    积分区间的起点取 ``settled_at()``，与聊天服务的 ``settle_elapsed`` 同口径——
    用 ``persona.get().updated_at`` 会把本文件要锁的那个缺陷一起复制进用例。
    """
    now = start
    for _ in range(steps):
        now += TEN_MINUTES_MS
        hours = (now - persona.settled_at()) / HOUR_MS
        persona.apply_elapsed(person_id, now, _rest_effect(hours))
        if with_turns:
            persona.apply_turn(person_id, now, weight=1.0)
    return persona.get(person_id).energy


def test_rest_accumulates_without_conversation(db: sqlite3.Connection) -> None:
    """无对话时休息照常入账：两小时 +4 点。"""
    person_id = _owner_id(db)
    persona = Persona(db)
    start = 10 * HOUR_MS
    _reset(db, person_id, 20.0, start)

    energy = _simulate(persona, person_id, start, 12, with_turns=False)

    assert energy == 20.0 + REST_ENERGY_PER_HOUR * 2


def test_conversation_does_not_swallow_rest(db: sqlite3.Connection) -> None:
    """有对话时休息同样入账，回合只扣自己那一份。

    改动前这里是 15.2——两小时休息的 +4 点被十二个回合逐次重置游标全部丢掉，
    只剩下回合自身的消耗。
    """
    person_id = _owner_id(db)
    persona = Persona(db)
    start = 10 * HOUR_MS
    _reset(db, person_id, 20.0, start)

    energy = _simulate(persona, person_id, start, 12, with_turns=True)

    expected = 20.0 + REST_ENERGY_PER_HOUR * 2 - TURN_ENERGY_COST * 12
    assert abs(energy - expected) < 1e-6


def test_turn_does_not_advance_settle_cursor(db: sqlite3.Connection) -> None:
    """回合结算不得推进时间结算游标——这是上一条能成立的机制判据。"""
    person_id = _owner_id(db)
    persona = Persona(db)
    start = 10 * HOUR_MS
    _reset(db, person_id, 50.0, start)

    persona.apply_turn(person_id, start + TEN_MINUTES_MS, weight=1.0)

    assert persona.settled_at() == start
    # 而人物侧的 updated_at 仍然跟随互动推进，profiles 面板依赖这条语义。
    assert persona.get(person_id).updated_at == start + TEN_MINUTES_MS


def test_settle_cursor_advances_only_when_applied(db: sqlite3.Connection) -> None:
    """不足一小时时早退且不推进游标，区间留给下一次累积。"""
    person_id = _owner_id(db)
    persona = Persona(db)
    start = 10 * HOUR_MS
    _reset(db, person_id, 50.0, start)

    half_hour = start + HOUR_MS // 2
    persona.apply_elapsed(person_id, half_hour, _rest_effect(0.5))
    assert persona.settled_at() == start, '不足一小时不应推进游标'
    assert persona.get(person_id).energy == 50.0

    full_hour = start + HOUR_MS
    persona.apply_elapsed(person_id, full_hour, _rest_effect(1.0))
    assert persona.settled_at() == full_hour
    assert persona.get(person_id).energy == 50.0 + REST_ENERGY_PER_HOUR


# --------------------------------------------------------- 离线空缺的结算边界

def _insert_activity(
    db: sqlite3.Connection,
    *,
    kind: str,
    energy_pace: int,
    started_at: int,
    expected_until: int,
    ended_at: int | None,
    source: str,
) -> None:
    """直接写一行活动，用来摆出「离线前留下一段未结束活动」的现场。"""
    db.execute(
        """INSERT INTO activities
           (kind, doing, mood, energy_pace, mood_pace, advances,
            started_at, expected_until, ended_at, source)
           VALUES (?, ?, ?, ?, 0, NULL, ?, ?, ?, ?)""",
        (kind, f'{kind} 活动', '', energy_pace, started_at,
         expected_until, ended_at, source),
    )
    db.commit()


def test_decided_until_stops_at_open_activity_horizon(db: sqlite3.Connection) -> None:
    """进行中的活动只算到 expected_until，之后的空缺不算已决策。"""
    from src.core.schedule.timeline import ActivityTimeline

    start = 10 * HOUR_MS
    _insert_activity(
        db, kind='awake', energy_pace=0, started_at=start,
        expected_until=start + HOUR_MS, ended_at=None, source='decided',
    )
    timeline = ActivityTimeline(db)

    # 离线九小时之后回来：已决策的终点仍然是那条活动的 expected_until。
    assert timeline.decided_until(start + 10 * HOUR_MS) == start + HOUR_MS
    # 还没走到边界时，终点就是当下。
    assert timeline.decided_until(start + HOUR_MS // 2) == start + HOUR_MS // 2


def test_offline_sleep_is_credited_after_backfill(db: sqlite3.Connection) -> None:
    """离线整夜的睡眠必须在补写之后仍能入账。

    改动前这一段永久丢失：结算同步跑在回合开头，把整段空缺按离线前那条清醒活动
    算掉并推进游标；补写是后台任务，等它把睡眠写进来时游标早已越过。
    """
    from src.core.schedule.timeline import ActivityTimeline

    person_id = _owner_id(db)
    persona = Persona(db)
    start = 10 * HOUR_MS
    _reset(db, person_id, 10.0, start)

    # 离线前：一段清醒活动，预计只到一小时后。
    _insert_activity(
        db, kind='awake', energy_pace=0, started_at=start,
        expected_until=start + HOUR_MS, ended_at=None, source='decided',
    )
    timeline = ActivityTimeline(db)

    # 九小时后回来，此刻补写尚未发生：只结算到已决策的终点。
    back = start + 10 * HOUR_MS
    frontier = timeline.decided_until(back)
    assert frontier == start + HOUR_MS
    persona.apply_elapsed(
        person_id, frontier, timeline.integrate_between(persona.settled_at(), frontier)
    )
    # 那一小时清醒 pace=0，按 ENERGY_RATE 扣一份。
    assert persona.get(person_id).energy == 10.0 - ENERGY_RATE
    assert persona.settled_at() == frontier

    # 后台补写落地：空缺被填成整夜睡眠。
    db.execute('UPDATE activities SET ended_at = ? WHERE ended_at IS NULL',
               (start + HOUR_MS,))
    _insert_activity(
        db, kind='sleep', energy_pace=3, started_at=start + HOUR_MS,
        expected_until=back, ended_at=back, source='backfilled',
    )

    # 下一次结算读到补写结果，九小时睡眠按 +4/小时入账。
    persona.apply_elapsed(
        person_id, back, timeline.integrate_between(persona.settled_at(), back)
    )
    assert persona.get(person_id).energy == (
        10.0 - ENERGY_RATE + ENERGY_RATE * 2 * 9
    )
    assert persona.settled_at() == back
