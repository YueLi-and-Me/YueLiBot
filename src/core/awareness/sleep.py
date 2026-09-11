"""把活动时间线转换为睡眠门控所需的薄状态。"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.runtime.clock import now as current_time
from src.core.schedule.timeline import ActivityTimeline

WAKE_GRACE_MS = 10 * 60_000
WAKE_TRANSITION_MS = 40 * 60_000


@dataclass(frozen=True)
class SleepState:
    """活动时间线对门控和表达层暴露的最小休息状态。"""

    asleep: bool
    just_woke: bool
    resting: bool


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

    def current(self, now: int | None = None) -> SleepState:
        """读取当前活动并派生 asleep、resting 与刚醒过渡状态。"""

        now = now if now is not None else current_time()
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
        if (
            activity.kind == 'awake'
            and activity.source == 'interrupted'
            and now - activity.started_at < WAKE_TRANSITION_MS
        ):
            self._woke_at = activity.started_at
        self._previously_asleep = asleep
        just_woke = (
            not asleep
            and self._woke_at is not None
            and 0 <= now - self._woke_at < WAKE_TRANSITION_MS
        )
        return SleepState(
            asleep=asleep,
            just_woke=just_woke,
            resting=activity.kind == 'rest',
        )

    def wake(self, now: int | None = None) -> SleepState:
        """立即打断 sleep 活动，并在宽限期内保持可回应。"""

        now = now if now is not None else current_time()
        self._woken_until = now + self._wake_grace_ms
        self._woke_at = now
        self._timeline.note_woken(now)
        self._previously_asleep = False
        return self.current(now)
