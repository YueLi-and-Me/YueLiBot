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

from src.core.logging.logger import get_logger
from src.core.llm_models.snapshot import bind_render_params
from src.core.persona.state import ENERGY_RATES, MOOD_RATE, ElapsedEffect
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

# 单段活动的时长上限（分钟），按 kind 区分：既封一次决策给出的时长，也封一段活动
# 自 started_at 起的累计时长；下限沿用 10 分钟不变。
#
# 现象：模型偶发给出远超人类尺度的单段时长。2026-09-10 的 id=344 一次决策 360 分钟，
#   内容是「被消息叫醒，拿着手机窝在床上迷迷糊糊刷刷b站或抖音缓一缓」，按 awake
#   pace=-1 的 -6.0/h 折算，这一个决策就扣掉 36 点精力，当天精力因此归零；id=341
#   同样 pace=-1、决策 321 分钟，一段扣 33.6 点。两段合计 -70，而全局活动日均净
#   收益只有 +15.0。
# 原因：写入层原先只校验 10 <= minutes <= 600，600 分钟对 sleep 合理、对 awake 过宽，
#   拦不住这类尾部离群值。
# 后果：长段是罕见离群值而非常态——近 14 天 106 次 awake 决策里 88% 不超过 60 分钟，
#   p90 为 1.62h≈97 分，超过 240 分钟的只有 2 段。因此上限按 p90 留出余量：awake
#   120 分钟只截断 106 段中的 3 段（2.8%）；rest 同期 75 次决策、p90 为 1.65h≈99 分，
#   180 分钟只截断 1 段；sleep 维持 600 分钟不变。上限只给尾部兜底，不用来改变
#   正常决策形态。
#
# 上限约束的是「一段活动的累计时长」，不只是「一次决策的时长」。
#
# 现象：2026-09-11 的 id=366 是 awake、pace=-1，12:07 起把 expected_until 一路推到
#   20:46，单段实际持续 8.64 小时，按 -6.0/h 折算一段扣掉约 52 点精力。单次决策上限
#   是 120 分钟，所以这一段至少被 continue 了 4 次，每一次单独看都合规。
# 原因：上限最初只判一次决策给出的 minutes，不判这一段自 started_at 以来的累计时长；
#   continue 只是 UPDATE 同一行的 expected_until，可以一次接一次地叠。
# 后果：只封单次决策等于没有上界，单段代价可以超过全天活动的净收益。因此提示词与
#   写入层都改判累计时长（_elapsed_minutes），并有意不设自动复制新段的兜底路径：
#   内容与 pace 不变的新段扣分与延续完全相同，只是把一段拆成两段。
#
# 键集合必须与 _ENERGY_PACE_RANGES 完全一致：两者由同一次 kind 校验共同消费。
_DECISION_MINUTE_LIMITS: dict[str, int] = {
    'awake': 120,
    'rest': 180,
    'sleep': 600,
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
    """一次调用给出的活动延续，或缺口补叙与下一段活动。"""

    next_activity: ActivityDraft | None = None
    continuation_minutes: int | None = None
    backfilled: Sequence[ActivityDraft] = ()

    def __post_init__(self) -> None:
        """保证一次转换只有一个动作，长缺口不能伪装成活动延续。"""

        has_next = self.next_activity is not None
        has_continuation = self.continuation_minutes is not None
        if has_next == has_continuation:
            raise ValueError('活动转换必须且只能选择延续或切换')
        if has_continuation:
            minutes = self.continuation_minutes
            if (
                isinstance(minutes, bool)
                or not isinstance(minutes, int)
                or not 10 <= minutes <= 600
            ):
                raise ValueError('活动延续 minutes 必须是 10 到 600 的整数')
            if self.backfilled:
                raise ValueError('长缺口补叙不能延续上一段活动')


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
    energy_enabled: bool = True


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


def _elapsed_minutes(activity: Activity, now: int) -> int:
    """返回一段活动自 ``started_at`` 起已经持续了多久。

    :param activity: 待测量的一段活动；只用它的 ``started_at``。
    :param now: 当前毫秒时间戳。
    :return: 向下取整的持续分钟数；``now`` 早于 ``started_at`` 时返回 0。

    这是**累计**时长，不是本次决策给出的时长：它把此前每一次 continue 叠加进来的
    时间一起算上，因此是判断「这一段还能不能继续延」的唯一正确依据。
    """

    return max(0, (now - activity.started_at) // MINUTE_MS)


def _clamp_decision_minutes(kind: str, minutes: int) -> int:
    """把一次决策给出的单段时长限幅到该 kind 的上限。

    :param kind: 活动类型，必须存在于 ``_DECISION_MINUTE_LIMITS``。
    :param minutes: 模型给出的单段时长，单位分钟；下限由调用方校验，此处只看上限。
    :return: 不超过该 kind 上限的时长；未越界时原样返回。

    副作用：
        越界时记录一条 warning，同时带上模型给出的原始值与限幅后的值。
    """

    limit = _DECISION_MINUTE_LIMITS[kind]
    if minutes <= limit:
        return minutes
    logger.warning(
        '活动 minutes 越界，已按 kind 限幅',
        kind=kind,
        raw=minutes,
        clamped=limit,
    )
    return limit


def _parse_draft(
    value: Any,
    *,
    intention_count: int,
    energy_enabled: bool,
    minutes_is_duration: bool,
) -> ActivityDraft | None:
    """严格解析单段活动；能量值域与单段时长由写入层限幅并记录告警。

    :param value: 模型给出的单个活动对象；任一字段缺失或类型不符即整体判非法。
    :param intention_count: 当轮意向条数，``advances`` 必须落在这一范围内。
    :param energy_enabled: 精力系统是否开启；关闭时拒绝 sleep。
    :param minutes_is_duration: ``minutes`` 是否就是这一段的真实时长。长缺口补叙的
        ``minutes`` 只表示各段之间的相对占比（真实时长由铺满缺口决定），此时不按
        kind 的上限限幅，否则一整夜的缺口会被判成非法输出。
    :return: 通过结构校验的活动草案；不合法时返回 ``None``。

    一次决策的时长越界只限幅、不判非法：模型给出 360 分钟的清醒段是常见偏差而非
    数据损坏，判非法会让整条决策作废（``_advance`` 转入 10 分钟重试），模型给出的原始值
    也一并丢失；限幅保留原始值供告警定位。
    """

    if not isinstance(value, dict):
        return None
    kind = value.get('kind')
    if kind not in _ENERGY_PACE_RANGES or (kind == 'sleep' and not energy_enabled):
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
    if minutes_is_duration:
        minutes = _clamp_decision_minutes(kind, minutes)
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
    energy_enabled: bool = True,
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
                energy_enabled=energy_enabled,
                minutes_is_duration=False,
            )
            if draft is None:
                return None
            backfilled.append(draft)
    else:
        decision = value.get('decision')
        if decision == 'continue':
            if set(value) != {'decision', 'minutes'}:
                return None
            continuation_minutes = _integer(value.get('minutes'))
            if continuation_minutes is None or not 10 <= continuation_minutes <= 600:
                return None
            return ActivityTransition(continuation_minutes=continuation_minutes)
        if decision != 'switch' or set(value) != {'decision', 'activity'}:
            return None
        next_value = value.get('activity')
        backfilled = []
    next_activity = _parse_draft(
        next_value,
        intention_count=intention_count,
        energy_enabled=energy_enabled,
        minutes_is_duration=True,
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


def _short_gap_rule(current: Activity, now: int) -> str:
    """给出短缺口这一轮允许的输出形态，累计达上限时不再提供延续选项。

    :param current: 当前进行中的活动；它的 ``kind`` 决定取哪一条上限。
    :param now: 当前毫秒时间戳。
    :return: 注入 ``backfill_rule`` 占位符的规则正文。

    判据是 ``now - current.started_at``，即这一段的累计时长。提示词里本来就写着
    「已经持续 X」，模型知情却仍选 continue，所以这里改的是**可选项本身**：一旦累计
    达到该 kind 的单段上限，continue 不再是一个合法输出，模型只能切换核心对象。
    """

    limit = _DECISION_MINUTE_LIMITS[current.kind]
    if _elapsed_minutes(current, now) < limit:
        return (
            '没有长缺口。先判断是继续当前活动，还是切换核心对象。只允许输出以下一种：\n'
            '{"decision":"continue","minutes":45}\n'
            '{"decision":"switch","activity":活动对象}\n'
            '不要输出 backfill，也不要直接输出裸活动对象。'
        )
    return (
        f'没有长缺口。当前这段已经达到单段时长上限（{limit} 分钟），必须切换核心对象：'
        '这一轮不允许延续当前活动，{"decision":"continue",...} 不是合法输出。'
        '只允许输出：\n'
        '{"decision":"switch","activity":活动对象}\n'
        '不要输出 backfill，也不要直接输出裸活动对象。'
    )


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
        backfill_rule = _short_gap_rule(current, now)
    sleep_rule = (
        '允许选择 sleep；真的睡着时才用 sleep，闭目养神但仍会回应要用 rest。'
        if context.energy_enabled
        else '当前精力系统已关闭，不允许选择 sleep；活动照常进行，不根据精力安排活动。'
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
            energy_enabled=context.energy_enabled,
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

    def decided_until(self, now: int) -> int:
        """返回活动时间线已经「定下来」的终点，供状态结算裁剪区间。

        :param now: 当前毫秒时间戳。
        :return: 不晚于 ``now`` 的时刻；进行中的活动只算到它的 ``expected_until``，
            再往后属于尚未决策的空缺。没有任何活动时返回 ``now``。

        **为什么结算不能一路推到 now。**

        - 现象：离线一整夜再上线，精力不但没有因为睡眠回升，反而更低。
        - 原因：越过 ``expected_until`` 的那段时间还没有被决策。``current()`` 只在
          后台任务里调模型补写它（``_advance`` → ``_apply_transition``），而状态结算
          是同步跑在回合开头的。结算先发生时，``integrate_between`` 会把整段空缺按
          离线前那条活动的 pace 算掉（``COALESCE(ended_at, to_ms)`` 让未结束的活动
          一直延伸到区间末端），随后游标推过这一段。
        - 后果：后台补写进来的真实活动——整夜睡眠是其中最大的一笔——再也不会被任何
          一次结算读到，那份恢复永久丢失；而空缺本身还被按清醒活动扣了分。

        因此结算只推进到本方法给出的终点，空缺留给下一次——等后台把它补写成真实
        活动之后再积分。
        """
        row = self._db.execute(
            """SELECT expected_until FROM activities
               WHERE ended_at IS NULL ORDER BY started_at DESC, id DESC LIMIT 1"""
        ).fetchone()
        if row is not None:
            return min(now, int(row[0]))
        row = self._db.execute(
            'SELECT MAX(ended_at) FROM activities WHERE ended_at IS NOT NULL'
        ).fetchone()
        if row is None or row[0] is None:
            return now
        return min(now, int(row[0]))

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
            energy_delta += ENERGY_RATES[(activity.kind, activity.energy_pace)] * hours
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
        """延续当前活动，或根据缺口长度补满缺口并写入下一段。

        :param previous: 触发这次决策的进行中活动；它的 ``started_at`` 与 ``kind``
            共同决定延续是否还在单段累计上限之内。
        :param transition: 决策器给出的转换；延续与切换互斥，由
            :class:`ActivityTransition` 保证。
        :param now: 当前毫秒时间戳；延续的 ``expected_until`` 与新段的 ``started_at``
            都以它为基准。
        :param gap_ms: ``now`` 超出上一条 ``expected_until`` 的毫秒数。
        :raises ValueError: 长缺口被要求延续、延续累计已达该 kind 的单段上限、
            切换缺少下一段活动，或补叙无法形成连续正时长时抛出。异常不在此处兜底：
            调用方 ``_advance`` 会记 `活动决策失败` 并续期重试。
        :return: 无返回值。
        副作用：写入 activities 表的 UPDATE 或 INSERT，并在返回前断言时间线不变量。
        """

        with self._db:
            still_open = self._db.execute(
                'SELECT 1 FROM activities WHERE id = ? AND ended_at IS NULL',
                (previous.id,),
            ).fetchone()
            if still_open is None:
                return
            if transition.continuation_minutes is not None:
                if gap_ms > SHORT_GAP_MS:
                    raise ValueError('长缺口不能延续上一段活动')
                limit = _DECISION_MINUTE_LIMITS[previous.kind]
                elapsed_minutes = _elapsed_minutes(previous, now)
                # 累计上限是硬判据，不只写在提示词里：模型可以无视提示词继续回
                # continue，而每一次单独的 continue 都不超单次上限。抛出后由
                # `_advance` 记 `活动决策失败` 并把 expected_until 续期
                # DECISION_RETRY_MS；续期生效期间 `current()` 不会再创建后台任务，
                # 所以周期性轮询不会把重试间隔压缩到每分钟一次。
                if elapsed_minutes >= limit:
                    raise ValueError(
                        f'累计超限：{previous.kind} 活动已持续 {elapsed_minutes} 分钟，'
                        f'达到单段时长上限 {limit} 分钟，不能再延续'
                    )
                # 延续同样是「一次决策的时长」：不封顶时它能把清醒段一路延到数小时，
                # 与切换出一条长段是同一个故障（见 _DECISION_MINUTE_LIMITS）。
                minutes = _clamp_decision_minutes(
                    previous.kind,
                    transition.continuation_minutes,
                )
                # 单次决策上限仍不足以定住实际段长：在一段已经持续 30 分钟的 awake 上
                # 再延续 120 分钟，实际段长是 150 分钟，照样越过累计上限。因此还要按
                # 这一段剩余的可用时长再截断一次。剩余不足 10 分钟时截断结果会小于
                # 模型输出的下限，这是有意的：这一段就停在累计上限上，下一次边界必然
                # 进入必须切换的分支。
                remaining_minutes = limit - elapsed_minutes
                if minutes > remaining_minutes:
                    logger.warning(
                        '延续时长超过这一段剩余的可用时长，已截断到累计上限',
                        activity_id=previous.id,
                        kind=previous.kind,
                        raw=transition.continuation_minutes,
                        clamped=remaining_minutes,
                        elapsed_minutes=elapsed_minutes,
                    )
                    minutes = remaining_minutes
                self._db.execute(
                    'UPDATE activities SET expected_until = ? WHERE id = ?',
                    (now + minutes * MINUTE_MS, previous.id),
                )
                # 提到 info 是排查需要：延续不新增时间线段，这条日志是「continue 真的
                # 发生过」的唯一证据。debug 级既不进 stdout（systemd 部署下 stdout 即
                # journal）、也不进文件日志，默认配置下等于不存在——上一轮排查正是
                # 因为 grep 不到它，把「continue 叠加」误判成「单次封顶失效」。
                logger.info(
                    '延续当前活动，不新增时间线段',
                    activity_id=previous.id,
                    minutes=minutes,
                    elapsed_minutes=elapsed_minutes,
                )
            else:
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
                next_activity = transition.next_activity
                if next_activity is None:
                    raise ValueError('活动切换缺少下一段活动')
                self._insert_draft(
                    next_activity,
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
        """校验、限幅并写入一段活动，返回新记录主键。

        ``energy_pace`` 与 ``mood_pace`` 按值域限幅，``minutes`` 在它决定段长时按
        ``_DECISION_MINUTE_LIMITS`` 限幅；三处越界都只记录 warning，不阻断写入。
        """

        if draft.kind not in _ENERGY_PACE_RANGES:
            raise ValueError(f'未知活动 kind：{draft.kind}')
        if not draft.doing.strip() or not draft.mood.strip():
            raise ValueError('活动 doing 和 mood 不能为空')
        if draft.minutes < 1:
            raise ValueError('活动 minutes 必须为正整数')
        pace_min, pace_max = _ENERGY_PACE_RANGES[draft.kind]
        energy_pace = min(pace_max, max(pace_min, draft.energy_pace))
        mood_pace = min(3, max(-3, draft.mood_pace))
        # 只有 minutes 真正决定段长时才限幅：补叙会传入 expected_until，那里的
        # minutes 只是铺满缺口的相对占比，本身就可能超出单段上限。
        minutes = (
            _clamp_decision_minutes(draft.kind, draft.minutes)
            if expected_until is None
            else draft.minutes
        )
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
        until = expected_until or started_at + minutes * MINUTE_MS
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
