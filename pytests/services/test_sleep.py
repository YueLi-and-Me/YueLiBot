"""睡眠薄门控与活动时间线的一致性测试。"""

from __future__ import annotations

from datetime import datetime

import sqlite3

from src.core.awareness.sleep import SleepStateController
from src.core.schedule.timeline import ActivityTimeline


def _timestamp(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute).timestamp() * 1000)


def _insert(
    db: sqlite3.Connection,
    *,
    kind: str,
    started_at: int,
    expected_until: int,
) -> int:
    cursor = db.execute(
        '''INSERT INTO activities
             (kind, doing, mood, energy_pace, mood_pace, advances,
              started_at, expected_until, ended_at, source)
           VALUES (?, ?, ?, ?, 0, NULL, ?, ?, NULL, 'decided')''',
        (
            kind,
            '睡觉' if kind == 'sleep' else '躺着休息',
            '睡着后不会回应' if kind == 'sleep' else '放松但仍然清醒',
            3 if kind == 'sleep' else 1,
            started_at,
            expected_until,
        ),
    )
    db.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def test_state_transition_persists_new_value_and_restores_round_trip(
    db: sqlite3.Connection,
) -> None:
    """唤醒写入活动事实，重建控制器后仍从同一时间线读到清醒。"""

    sleep_at = _timestamp(2026, 8, 7, 23, 30)
    wake_at = _timestamp(2026, 8, 8, 8, 0)
    sleep_id = _insert(
        db,
        kind='sleep',
        started_at=sleep_at,
        expected_until=wake_at + 60_000,
    )
    timeline = ActivityTimeline(db)
    first = SleepStateController(timeline)

    assert first.current(sleep_at).asleep is True
    state = first.wake(wake_at)

    assert state.asleep is False
    interrupted = db.execute(
        'SELECT ended_at, source FROM activities WHERE id = ?',
        (sleep_id,),
    ).fetchone()
    assert interrupted is not None
    assert tuple(interrupted) == (wake_at, 'interrupted')

    restored = SleepStateController(ActivityTimeline(db))
    restored_state = restored.current(wake_at + 1_000)
    assert restored_state.asleep is False
    assert restored_state.just_woke is True


def test_inconsistent_persisted_state_is_normalized_before_evaluation(
    db: sqlite3.Connection,
) -> None:
    """rest 是活动事实但不是睡眠；门控无需维护第二份可矛盾的运行态。"""

    now = _timestamp(2026, 8, 8, 14, 0)
    _insert(
        db,
        kind='rest',
        started_at=now,
        expected_until=now + 30 * 60_000,
    )

    state = SleepStateController(ActivityTimeline(db)).current(now)

    assert state.asleep is False
    assert state.resting is True
    assert state.just_woke is False
