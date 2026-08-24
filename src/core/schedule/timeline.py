"""连续生活活动时间线。

时间线只记录实际决策与事后补叙，不把每日方向当成已经发生的事实。同步读取始终
立即返回数据库里的当前段；到达边界后的模型决策由后台任务推进。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Protocol, Sequence

import asyncio
import json
import sqlite3

from src.core.common.logger import get_logger
from src.core.llm_models.snapshot import bind_render_params
from src.core.persona.state import MOOD_RATE, ElapsedEffect
from src.core.prompts.registry import get_prompt

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


class ActivityGenerator(Protocol):
    """活动决策所需的最小模型生成接口。"""

    async def generate(self, prompt: str) -> str:
        """返回一份 JSON object 正文。"""

        ...


@dataclass(frozen=True)
class ActivityDecisionContext:
    """运行时已经知道、可供下一步活动判断的全部上下文。"""

    character_name: str
    character_personality: str
    persona: str
    sleep_history: str
    intentions: str
    intention_count: int
    rough_rhythm: str
    recent_activities: str
    interaction: str
    sleep_enabled: bool = True


def _required_text(value: Any, *, maximum: int) -> str | None:
    """校验模型输出中的必填单行文本，不做内容补写。"""

    if not isinstance(value, str):
        return None
    normalized = ' '.join(value.split())
    if not normalized or len(normalized) > maximum:
        return None
    return normalized


def _integer(value: Any) -> int | None:
    """只接收真正的 JSON 整数，排除 ``bool`` 与字符串隐式转换。"""

    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_draft(
    value: Any,
    *,
    intention_count: int,
    sleep_enabled: bool,
) -> ActivityDraft | None:
    """严格解析单段活动；能量值域由写入层负责限幅并记录告警。"""

    if not isinstance(value, dict):
        return None
    kind = value.get('kind')
    if kind not in _ENERGY_PACE_RANGES or (kind == 'sleep' and not sleep_enabled):
        return None
    doing = _required_text(value.get('doing'), maximum=80)
    mood = _required_text(value.get('mood'), maximum=80)
    energy_pace = _integer(value.get('energyPace'))
    mood_pace = _integer(value.get('moodPace'))
    minutes = _integer(value.get('minutes'))
    advances_value = value.get('advances')
    advances = _integer(advances_value) if advances_value is not None else None
    if (
        doing is None
        or mood is None
        or energy_pace is None
        or mood_pace is None
        or minutes is None
        or not 10 <= minutes <= 600
        or not -3 <= mood_pace <= 3
        or (
            advances is not None
            and not 1 <= advances <= intention_count
        )
    ):
        return None
    return ActivityDraft(
        kind=kind,
        doing=doing,
        mood=mood,
        energy_pace=energy_pace,
        mood_pace=mood_pace,
        minutes=minutes,
        advances=advances,
    )


def parse_activity_decision(
    raw: str,
    *,
    intention_count: int,
    require_backfill: bool,
    sleep_enabled: bool = True,
) -> ActivityTransition | None:
    """解析一次下一步活动决策，长缺口必须同时提供补叙。

    ``doing`` 有意不做钟点过滤：钟点只禁止出现在全天规划层，一次具体活动可以写
    “十一点去交作业”。非法结构整体返回 ``None``，不局部猜测或补默认值。
    """

    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or intention_count < 0:
        return None
    if require_backfill:
        backfill_value = value.get('backfill')
        if not isinstance(backfill_value, list) or not backfill_value:
            return None
        next_value = value.get('next')
        backfilled: list[ActivityDraft] = []
        for item in backfill_value:
            draft = _parse_draft(
                item,
                intention_count=intention_count,
                sleep_enabled=sleep_enabled,
            )
            if draft is None:
                return None
            backfilled.append(draft)
    else:
        next_value = value
        backfilled = []
    next_activity = _parse_draft(
        next_value,
        intention_count=intention_count,
        sleep_enabled=sleep_enabled,
    )
    if next_activity is None:
        return None
    return ActivityTransition(
        next_activity=next_activity,
        backfilled=tuple(backfilled),
    )


def _duration_text(duration_ms: int) -> str:
    """把毫秒时长压成适合提示词阅读的中文小时/分钟描述。"""

    total_minutes = max(0, duration_ms // MINUTE_MS)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f'{hours} 小时 {minutes} 分'
    if hours:
        return f'{hours} 小时'
    return f'{minutes} 分钟'


def build_activity_prompt(
    current: Activity,
    now: int,
    gap_ms: int,
    context: ActivityDecisionContext,
    *,
    render_params: dict[str, dict[str, str]] | None = None,
) -> str:
    """渲染下一步活动提示词，长缺口与即时决策共用一次调用。"""

    current_dt = datetime.fromtimestamp(now / 1000)
    weekday = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][
        current_dt.weekday()
    ]
    if gap_ms > SHORT_GAP_MS:
        backfill_rule = (
            f'上一条预期在 {datetime.fromtimestamp(current.expected_until / 1000):%m-%d %H:%M} '
            f'结束，到现在有 {_duration_text(gap_ms)} 没有记录。输出 '
            '{"backfill":[活动对象...],"next":活动对象}；backfill 必须按顺序覆盖整段缺口，'
            '时长只表示各段相对占比，系统会把它们连续铺满。'
        )
    else:
        backfill_rule = '没有长缺口。直接输出一个活动对象，不要包 next，不要输出 backfill。'
    sleep_rule = (
        '允许选择 sleep；真的睡着时才用 sleep，闭目养神但仍会回应要用 rest。'
        if context.sleep_enabled
        else '当前配置不允许选择 sleep；需要恢复时只能选择 rest，并保持可回应。'
    )
    values = {
        'character_name': context.character_name,
        'character_personality': context.character_personality,
        'time_context': f'{current_dt:%Y-%m-%d %H:%M}，{weekday}',
        'current_activity': (
            f'{current.doing}，已经持续 {_duration_text(now - current.started_at)}；'
            f'原本打算持续到 {datetime.fromtimestamp(current.expected_until / 1000):%H:%M}'
        ),
        'persona': context.persona,
        'sleep_history': context.sleep_history,
        'intentions': context.intentions,
        'rough_rhythm': context.rough_rhythm,
        'recent_activities': context.recent_activities,
        'interaction': context.interaction,
        'backfill_rule': backfill_rule,
        'sleep_rule': sleep_rule,
    }
    if render_params is not None:
        render_params['activity.next'] = values
    return get_prompt('activity.next').render(**values)


class ActivityDecisionService:
    """用运行时连续状态生成下一段活动，不持有时间线写权限。"""

    def __init__(
        self,
        generator: ActivityGenerator,
        context: Callable[[int], ActivityDecisionContext],
    ) -> None:
        self._generator = generator
        self._context = context

    async def decide(
        self,
        current: Activity,
        now: int,
        gap_ms: int,
    ) -> ActivityTransition:
        """生成并严格解析一次活动转换；非法结果直接暴露给时间线续期。"""

        context = self._context(now)
        render_params: dict[str, dict[str, str]] = {}
        prompt = build_activity_prompt(
            current,
            now,
            gap_ms,
            context,
            render_params=render_params,
        )
        bind_render_params(render_params)
        raw = await self._generator.generate(prompt)
        parsed = parse_activity_decision(
            raw,
            intention_count=context.intention_count,
            require_backfill=gap_ms > SHORT_GAP_MS,
            sleep_enabled=context.sleep_enabled,
        )
        if parsed is None:
            raise ValueError('下一步活动模型输出未通过结构校验')
        return parsed


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

    def set_decider(self, decider: ActivityDecider) -> None:
        """在组合根完成上下文服务装配后绑定唯一活动决策器。"""

        self._decider = decider

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

    def recent_summary(self, now: int, limit: int = 6) -> str:
        """按真实时间线概括最近若干段活动，供下一步决策回看。"""

        rows = self._db.execute(
            """SELECT doing FROM activities
               WHERE started_at <= ? ORDER BY started_at DESC, id DESC LIMIT ?""",
            (now, limit),
        ).fetchall()
        activities = [str(row[0]) for row in reversed(rows)]
        return ' → '.join(activities) if activities else '还没有活动记录'

    def last_sleep_summary(self, now: int) -> str:
        """描述最近一段真实睡眠的结束距离和持续时长。"""

        row = self._db.execute(
            """SELECT * FROM activities
               WHERE kind = 'sleep' AND started_at <= ?
               ORDER BY started_at DESC, id DESC LIMIT 1""",
            (now,),
        ).fetchone()
        if row is None:
            return '还没有睡眠记录'
        activity = _activity_from_row(row)
        sleep_end = min(now, activity.ended_at or now)
        duration = _duration_text(sleep_end - activity.started_at)
        if activity.ended_at is None:
            return f'这一觉已经睡了 {duration}'
        return f'{_duration_text(now - activity.ended_at)}前结束，睡了 {duration}'

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
                expected_until=now,
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
