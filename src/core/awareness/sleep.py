"""把活动时间线转换为睡眠门控所需的薄状态，并记录本进程观察到的深睡区间。

门控、表达层与起床汇总都只读取这里产出的 :class:`SleepState`；控制器不反向修改
活动内容，唯一写操作是外部唤醒时调用 :meth:`ActivityTimeline.note_woken`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.core.runtime.clock import now as current_time
from src.core.schedule.timeline import ActivityTimeline

SleepLevel = Literal['awake', 'drowsy', 'light', 'deep']

WAKE_GRACE_MS = 10 * 60_000
WAKE_TRANSITION_MS = 40 * 60_000


@dataclass(frozen=True)
class DeepSleepPeriod:
    """本进程在线观察到的一段深睡，边界为毫秒时间戳。

    :ivar activity_id: 对应 ``activities.id``，用于同一次起床的去重。
    :ivar started_at: 本进程实际观察到的深睡起点，不早于进程首次读取时间线。
    :ivar ended_at: 该深睡活动段结束时间；只记录已结束的段，保证汇总窗口闭合。
    """

    activity_id: int
    started_at: int
    ended_at: int


@dataclass(frozen=True)
class SleepState:
    """活动时间线对门控和表达层暴露的最小休息状态。

    :ivar deep_sleep_periods: 本次刚醒前观察到的深睡区间；只在 ``just_woke`` 为真
        时非空。进程启动前已经发生的时间不纳入，避免把离线前的消息当成待汇总。
    """

    asleep: bool
    just_woke: bool
    resting: bool
    level: SleepLevel = 'awake'
    activity_id: int | None = None
    deep_sleep_periods: tuple[DeepSleepPeriod, ...] = ()


class SleepStateController:
    """从当前活动读取睡眠事实，并处理外部唤醒宽限。"""

    def __init__(
        self,
        timeline: ActivityTimeline,
        wake_grace_ms: int = WAKE_GRACE_MS,
        *,
        energy_enabled: bool = True,
    ) -> None:
        """绑定唯一活动时间线；构造阶段不读库、不创建后台任务。"""

        self._timeline = timeline
        self._energy_enabled = energy_enabled
        self._wake_grace_ms = wake_grace_ms
        self._woken_until = 0
        self._woke_at: int | None = None
        self._previously_asleep = False
        # 第一次读取时间线的时间；深睡窗口的左边界从这里开始，进程启动前的
        # 消息与睡眠都不算「被看见过」。
        self._observed_since: int | None = None
        # 正在进行或已结束但尚未随起床消费的深睡活动：activity id -> 观察起点。
        self._observed_deep: dict[int, int] = {}
        # 最近一次真实醒转时收集到的深睡区间，随 just_woke 状态只暴露一轮。
        self._wake_periods: tuple[DeepSleepPeriod, ...] = ()

    def current(self, now: int | None = None) -> SleepState:
        """读取当前活动并派生 asleep、resting 与刚醒过渡状态。

        :param now: 可选的当前毫秒时间戳；省略时读取统一时钟。
        :return: 当前睡眠状态；刚醒时携带本进程观察到的深睡区间。
        副作用：可能通过时间线触发边界决策或外部唤醒写回，并更新进程内观察状态。
        """

        now = now if now is not None else current_time()
        if self._observed_since is None:
            self._observed_since = now
        activity = self._timeline.current(now)
        asleep = activity.kind == 'sleep'
        if asleep and (not self._energy_enabled or now < self._woken_until):
            # 关闭精力时也要结束已存在的睡眠段，避免旧状态继续限制回复。
            # 外部唤醒与恰好完成的后台决策可能交错；宽限期内再次把新 sleep 段打断，
            # 确保刚回复用户后不会立即回到睡着状态。
            self._timeline.note_woken(now)
            activity = self._timeline.current(now)
            asleep = False
        if self._previously_asleep and not asleep:
            self._woke_at = now
            self._wake_periods = self._collect_deep_sleep_periods(now)
            self._observed_deep.clear()
        if (
            activity.kind == 'awake'
            and activity.source == 'interrupted'
            and now - activity.started_at < WAKE_TRANSITION_MS
        ):
            self._woke_at = activity.started_at
        if asleep and activity.energy_pace == 3:
            # 深睡只认本进程实际观察到的部分：若进程启动时这段深睡已经开始，
            # 起点取观察起点；同一活动段重复读取不重复记录。
            self._observed_deep.setdefault(
                activity.id,
                max(activity.started_at, self._observed_since),
            )
        self._previously_asleep = asleep
        just_woke = (
            not asleep
            and self._woke_at is not None
            and 0 <= now - self._woke_at < WAKE_TRANSITION_MS
        )
        level: SleepLevel = 'awake'
        if asleep:
            level = 'deep' if activity.energy_pace == 3 else 'light'
        elif activity.kind == 'rest':
            level = 'drowsy'
        return SleepState(
            asleep=asleep,
            just_woke=just_woke,
            resting=activity.kind == 'rest',
            level=level,
            activity_id=activity.id,
            # 只有刚醒后的过渡窗口才交付深睡区间；窗口结束后状态保持为空，避免
            # 每轮对话都拿着旧区间重复触发汇总。
            deep_sleep_periods=self._wake_periods if just_woke else (),
        )

    def _collect_deep_sleep_periods(self, now: int) -> tuple[DeepSleepPeriod, ...]:
        """收集本进程观察到、且在本次醒转前已结束的深睡区间。

        :param now: 醒转发生的毫秒时间戳，作为窗口右边界。
        :return: 按活动开始顺序排列的深睡区间；没有观察记录时为空元组。
        副作用：只读时间线。
        """

        observed_since = self._observed_since
        if observed_since is None:
            return ()
        periods: list[DeepSleepPeriod] = []
        for item in self._timeline.between(observed_since, now):
            started_at = self._observed_deep.get(item.id)
            if started_at is None or item.ended_at is None or item.ended_at <= started_at:
                continue
            periods.append(DeepSleepPeriod(item.id, started_at, item.ended_at))
        return tuple(periods)

    def wake(self, now: int | None = None) -> SleepState:
        """立即打断 sleep 活动，并在宽限期内保持可回应。

        外部唤醒也要走与自然醒转同一套深睡区间收集，否则刚被消息叫醒时
        ``just_woke`` 已为真、汇总窗口却还是空的。只有真实处于睡眠中时才允许
        保留本轮的区间；不在睡眠中调用唤醒不能把上一轮的区间残留到下一次。
        """

        now = now if now is not None else current_time()
        self._woken_until = now + self._wake_grace_ms
        self._woke_at = now
        self._timeline.note_woken(now)
        if not self._previously_asleep:
            self._wake_periods = ()
        return self.current(now)
