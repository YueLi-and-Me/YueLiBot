"""连续生活活动时间线。

时间线只记录实际决策与事后补叙，不把每日方向当成已经发生的事实。同步读取始终
立即返回数据库里的当前段；到达边界后的模型决策由后台任务推进。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Iterable, Sequence

import asyncio
import sqlite3

from src.core.common.logger import get_logger
from src.core.persona.state import MOOD_RATE, ElapsedEffect

logger = get_logger(__name__)

HOUR_MS = 60 * 60_000
MINUTE_MS = 60_000
SHORT_GAP_MS = 90 * MINUTE_MS
DECISION_RETRY_MS = 10 * MINUTE_MS

_ENERGY_PACE_RANGES: dict[str, tuple[int, int]] = {
    'awake': (-3, 1),
    'rest': (1, 2),
    'sleep': (2, 3),
}


@dataclass(frozen=True)
class Activity:
    """一段已落库的实际活动。"""

    id: int
    kind: str
    doing: str
    mood: str
    energy_pace: int
    mood_pace: int
    advances: int | None
    started_at: int
    expected_until: int
    ended_at: int | None
    source: str


@dataclass(frozen=True)
class ActivityDraft:
    """活动决策产生、尚未分配绝对时间的候选段。"""

    kind: str
    doing: str
    mood: str
    energy_pace: int
    mood_pace: int
    minutes: int
    advances: int | None = None


@dataclass(frozen=True)
class ActivityTransition:
    """一次调用同时给出的缺口补叙与下一段活动。"""

    next_activity: ActivityDraft
    backfilled: Sequence[ActivityDraft] = ()


ActivityDecider = Callable[[Activity, int, int], Awaitable[ActivityTransition]]


def _activity_from_row(row: sqlite3.Row) -> Activity:
    """把 SQLite 行转换成不可变活动对象。"""

    return Activity(
        id=int(row['id']),
        kind=str(row['kind']),
        doing=str(row['doing']),
        mood=str(row['mood']),
        energy_pace=int(row['energy_pace']),
        mood_pace=int(row['mood_pace']),
        advances=int(row['advances']) if row['advances'] is not None else None,
        started_at=int(row['started_at']),
        expected_until=int(row['expected_until']),
        ended_at=int(row['ended_at']) if row['ended_at'] is not None else None,
        source=str(row['source']),
    )


def _neutral_activity(now: int) -> Activity:
    """数据库不可读时也能同步返回的中性清醒状态。"""

    return Activity(
        id=0,
        kind='awake',
        doing='刚停下来，还没决定接下来做什么',
        mood='状态平稳，仍会正常回应',
        energy_pace=0,
        mood_pace=0,
        advances=None,
        started_at=now,
        expected_until=now + DECISION_RETRY_MS,
        ended_at=None,
        source='decided',
    )


class ActivityTimeline:
    """提供当前活动、外部唤醒与状态积分的单一事实来源。"""

    def __init__(
        self,
        db: sqlite3.Connection,
        decider: ActivityDecider | None = None,
    ) -> None:
        self._db = db
        self._decider = decider
        self._inflight: asyncio.Task[None] | None = None

    def current(self, now: int) -> Activity:
        """同步返回当前活动；边界决策只在后台进行，异常不会进入调用链。"""

        try:
            activity = self._open_activity()
            if activity is None:
                activity = self._insert_cold_start(now)
            if now >= activity.expected_until:
                self._ensure_background(activity, now)
            return activity
        except Exception:
            logger.exception('读取当前活动失败', now=now)
            return _neutral_activity(now)

    def note_woken(self, now: int) -> None:
        """收到外部唤醒事件时立即结束睡眠，并建立一段可回应的清醒活动。"""

        try:
            current = self._open_activity()
            if current is None or current.kind != 'sleep':
                return
            with self._db:
                self._db.execute(
                    "UPDATE activities SET ended_at = ?, source = 'interrupted' WHERE id = ?",
                    (now, current.id),
                )
                self._insert_draft(
                    ActivityDraft(
                        kind='awake',
                        doing='被消息叫醒，正慢慢清醒过来',
                        mood='刚醒时反应会慢一点，但会回应',
                        energy_pace=0,
                        mood_pace=0,
                        minutes=10,
                    ),
                    started_at=now,
                    ended_at=None,
                    source='interrupted',
                )
        except Exception:
            logger.exception('外部唤醒写入活动时间线失败', now=now)

    def integrate_between(self, from_ms: int, to_ms: int) -> ElapsedEffect:
        """按活动与目标区间的真实交集积分精力和心情变化。"""

        if to_ms <= from_ms:
            return ElapsedEffect(energy_delta=0.0, mood_delta=0.0)
        rows = self._db.execute(
            """SELECT * FROM activities
               WHERE started_at < ? AND COALESCE(ended_at, ?) > ?
               ORDER BY started_at, id""",
            (to_ms, to_ms, from_ms),
        ).fetchall()
        energy_delta = 0.0
        mood_delta = 0.0
        for row in rows:
            activity = _activity_from_row(row)
            segment_start = max(from_ms, activity.started_at)
            segment_end = min(to_ms, activity.ended_at or to_ms)
            if segment_end <= segment_start:
                continue
            hours = (segment_end - segment_start) / HOUR_MS
            energy_delta += 2.0 * (activity.energy_pace - 1) * hours
            mood_delta += MOOD_RATE * activity.mood_pace * hours
        return ElapsedEffect(energy_delta=energy_delta, mood_delta=mood_delta)

    def assert_invariants(self) -> None:
        """断言整条活动时间线连续，且只有最后一段可以保持进行中。"""

        rows = self._db.execute(
            'SELECT * FROM activities ORDER BY started_at, id'
        ).fetchall()
        activities = [_activity_from_row(row) for row in rows]
        open_indexes = [
            index for index, activity in enumerate(activities)
            if activity.ended_at is None
        ]
        if len(open_indexes) > 1 or (open_indexes and open_indexes[0] != len(activities) - 1):
            raise RuntimeError('活动时间线至多一条进行中记录，且必须是最后一条')
        for previous, following in zip(activities, activities[1:]):
            if previous.ended_at is None:
                raise RuntimeError('活动时间线的进行中记录后面仍有活动')
            if previous.ended_at < following.started_at:
                raise RuntimeError('活动时间线必须不留洞')
            if previous.ended_at > following.started_at:
                raise RuntimeError('活动时间线必须不重叠')

    def advanced_intention_indexes(self, date: str) -> set[int]:
        """返回指定自然日内被活动明确推进过的意向序号。"""

        start_dt = datetime.fromisoformat(date)
        start = int(start_dt.timestamp() * 1000)
        end = int((start_dt + timedelta(days=1)).timestamp() * 1000)
        rows = self._db.execute(
            """SELECT DISTINCT advances FROM activities
               WHERE advances IS NOT NULL
                 AND started_at < ?
                 AND COALESCE(ended_at, expected_until) > ?""",
            (end, start),
        ).fetchall()
        return {int(row[0]) for row in rows}

    def between(self, from_ms: int, to_ms: int) -> list[Activity]:
        """读取与指定时间区间相交的真实活动，供历史和观测层使用。"""

        rows = self._db.execute(
            """SELECT * FROM activities
               WHERE started_at < ? AND COALESCE(ended_at, expected_until) > ?
               ORDER BY started_at, id""",
            (to_ms, from_ms),
        ).fetchall()
        return [_activity_from_row(row) for row in rows]

    def _open_activity(self) -> Activity | None:
        """读取唯一进行中的活动。"""

        row = self._db.execute(
            'SELECT * FROM activities WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1'
        ).fetchone()
        return _activity_from_row(row) if row is not None else None

    def _insert_cold_start(self, now: int) -> Activity:
        """空时间线从中性清醒段开始，不伪造启动前发生过的事。"""

        draft = ActivityDraft(
            kind='awake',
            doing='刚停下来，还没决定接下来做什么',
            mood='状态平稳，仍会正常回应',
            energy_pace=0,
            mood_pace=0,
            minutes=10,
        )
        with self._db:
            activity_id = self._insert_draft(
                draft,
                started_at=now,
                ended_at=None,
                source='decided',
            )
        row = self._db.execute(
            'SELECT * FROM activities WHERE id = ?',
            (activity_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError('冷启动活动写入后无法读回')
        return _activity_from_row(row)

    def _ensure_background(self, activity: Activity, now: int) -> None:
        """在运行中的事件循环里创建唯一的边界决策任务。"""

        if self._decider is None or self._inflight is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning('当前没有事件循环，活动决策留到下次读取', activity_id=activity.id)
            return
        task = loop.create_task(self._advance(activity, now))
        self._inflight = task
        task.add_done_callback(self._finish_background)

    def _finish_background(self, task: asyncio.Task[None]) -> None:
        """清理后台任务，并消费所有异常避免事件循环告警。"""

        if self._inflight is task:
            self._inflight = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            # `_advance` 自身已处理失败；这里是最后一道任务异常观测边界。
            logger.exception('活动后台决策出现未处理异常')

    async def _advance(self, activity: Activity, now: int) -> None:
        """调用决策器并原子写入连续的新时间线；失败时只延长当前段。"""

        if self._decider is None:
            return
        gap_ms = max(0, now - activity.expected_until)
        try:
            transition = await self._decider(activity, now, gap_ms)
            self._apply_transition(activity, transition, now, gap_ms)
        except Exception as exc:
            logger.exception(
                '活动决策失败，沿用当前活动并延后重试',
                activity_id=activity.id,
                error=str(exc),
            )
            try:
                with self._db:
                    self._db.execute(
                        """UPDATE activities
                           SET expected_until = ?
                           WHERE id = ? AND ended_at IS NULL""",
                        (now + DECISION_RETRY_MS, activity.id),
                    )
            except Exception:
                logger.exception('活动决策失败后的续期也写入失败', activity_id=activity.id)

    def _apply_transition(
        self,
        previous: Activity,
        transition: ActivityTransition,
        now: int,
        gap_ms: int,
    ) -> None:
        """根据缺口长度结束旧段、补满缺口并写入下一段。"""

        with self._db:
            still_open = self._db.execute(
                'SELECT 1 FROM activities WHERE id = ? AND ended_at IS NULL',
                (previous.id,),
            ).fetchone()
            if still_open is None:
                return
            if gap_ms <= SHORT_GAP_MS:
                self._db.execute(
                    'UPDATE activities SET ended_at = ? WHERE id = ?',
                    (now, previous.id),
                )
            else:
                self._db.execute(
                    'UPDATE activities SET ended_at = ? WHERE id = ?',
                    (previous.expected_until, previous.id),
                )
                self._insert_backfill(
                    transition.backfilled,
                    previous.expected_until,
                    now,
                )
            self._insert_draft(
                transition.next_activity,
                started_at=now,
                ended_at=None,
                source='decided',
            )
        self.assert_invariants()

    def _insert_backfill(
        self,
        drafts: Sequence[ActivityDraft],
        start: int,
        end: int,
    ) -> None:
        """按模型给出的相对时长把长缺口完整且连续地铺满。"""

        if not drafts:
            raise ValueError('长缺口决策必须给出至少一段 backfill')
        total_minutes = sum(max(1, draft.minutes) for draft in drafts)
        cursor = start
        duration = end - start
        for index, draft in enumerate(drafts):
            segment_end = end if index == len(drafts) - 1 else (
                cursor + duration * max(1, draft.minutes) // total_minutes
            )
            if segment_end <= cursor:
                raise ValueError('backfill 段过多，无法形成正时长的连续活动')
            self._insert_draft(
                draft,
                started_at=cursor,
                ended_at=segment_end,
                source='backfilled',
                expected_until=segment_end,
            )
            duration -= segment_end - cursor
            total_minutes -= max(1, draft.minutes)
            cursor = segment_end

    def _insert_draft(
        self,
        draft: ActivityDraft,
        *,
        started_at: int,
        ended_at: int | None,
        source: str,
        expected_until: int | None = None,
    ) -> int:
        """校验、限幅并写入一段活动，返回新记录主键。"""

        if draft.kind not in _ENERGY_PACE_RANGES:
            raise ValueError(f'未知活动 kind：{draft.kind}')
        if not draft.doing.strip() or not draft.mood.strip():
            raise ValueError('活动 doing 和 mood 不能为空')
        if draft.minutes < 1:
            raise ValueError('活动 minutes 必须为正整数')
        pace_min, pace_max = _ENERGY_PACE_RANGES[draft.kind]
        energy_pace = min(pace_max, max(pace_min, draft.energy_pace))
        mood_pace = min(3, max(-3, draft.mood_pace))
        if energy_pace != draft.energy_pace:
            logger.warning(
                '活动 energyPace 越界，已按 kind 限幅',
                kind=draft.kind,
                raw=draft.energy_pace,
                clamped=energy_pace,
            )
        if mood_pace != draft.mood_pace:
            logger.warning(
                '活动 moodPace 越界，已限幅',
                raw=draft.mood_pace,
                clamped=mood_pace,
            )
        until = expected_until or started_at + draft.minutes * MINUTE_MS
        cursor = self._db.execute(
            """INSERT INTO activities
                 (kind, doing, mood, energy_pace, mood_pace, advances,
                  started_at, expected_until, ended_at, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                draft.kind,
                draft.doing.strip(),
                draft.mood.strip(),
                energy_pace,
                mood_pace,
                draft.advances,
                started_at,
                until,
                ended_at,
                source,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError('活动写入后没有主键')
        return int(cursor.lastrowid)
