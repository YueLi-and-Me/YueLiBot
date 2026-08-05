"""
睡眠概率评估与状态机。直接移植自 src/core/awareness/sleep.ts。

双 sigmoid 乘积模型：就寝端上升 × 起床端下降，无距离折返。
滞回门槛：入睡需 ≥ cutoff，醒来需 < wakeHysteresis < cutoff，避免边界抖动。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Optional

from yueli.common.clock import now as current_time
from yueli.schedule.plan import planned_sleep_window

WAKE_GRACE_MS = 10 * 60_000
WAKE_TRANSITION_MS = 40 * 60_000
_RECENT_CHAT_MS = 25 * 60_000
_DROWSY_PROBABILITY = 0.3
_WAKE_HYSTERESIS = 0.22
_SLEEP_RUNTIME_KEY = 'sleep_runtime'
_MINUTE_MS = 60_000
_LATE_SLEEP_COMPENSATION = 0.5
_SLEEP_SIGMOID_MINUTES = 42


@dataclass
class SleepState:
    asleep: bool
    drowsy: bool
    just_woke: bool
    probability: float


@dataclass
class SleepInputs:
    date: str
    bedtime_hint: str
    wake_hint: str
    energy: float
    last_interaction_at: int | None


@dataclass
class SleepEvaluation(SleepState):
    cutoff: float = 0.0
    minutes_from_bedtime: float = 0.0
    effective_wake_at: int = 0
    natural_wake_target_at: int = 0
    sleep_debt_delay_minutes: float = 0.0


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _logit(p: float) -> float:
    if p <= 0 or p >= 1:
        raise ValueError(f'睡眠概率门槛必须位于 0 到 1 之间：{p}')
    return math.log(p / (1 - p))


def sleep_jitter(date: str) -> float:
    """日期哈希，给每一天一个稳定的小抖动。"""
    h = 2166136261
    for ch in date:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return (h % 10_000) / 10_000


def sleep_jitter_minutes(date: str) -> float:
    return sleep_jitter(date) * 24 - 12


def evaluate_sleep(
    inputs: SleepInputs,
    now: int,
    previously_asleep: bool,
    jitter: float | None = None,
    wake_hysteresis: float | None = None,
    sleep_started_at: int | None = None,
) -> SleepEvaluation:
    from yueli.schedule.plan import DayPlan, DayPlanSlot, clock_minutes
    # Re-use planned_sleep_window by constructing a minimal DayPlan
    # We need bedtime_hint and wake_hint from SleepInputs
    from yueli.schedule.plan import fallback_day_plan
    plan = fallback_day_plan(inputs.date)
    plan.bedtime_hint = inputs.bedtime_hint
    plan.wake_hint = inputs.wake_hint
    bedtime_at, wake_at = planned_sleep_window(plan)

    stable_jitter = jitter if jitter is not None else sleep_jitter(inputs.date)
    cutoff = 0.72 + (stable_jitter - 0.5) * 0.12
    effective_wake_hysteresis = wake_hysteresis if wake_hysteresis is not None else _WAKE_HYSTERESIS

    energy_shift = (50 - inputs.energy) * 1.8
    since_interaction = float('inf') if inputs.last_interaction_at is None else (now - inputs.last_interaction_at)
    if since_interaction <= 0:
        chat_delay = -75.0
    elif since_interaction < _RECENT_CHAT_MS:
        chat_delay = -75 * (1 - since_interaction / _RECENT_CHAT_MS)
    else:
        chat_delay = 0.0

    jitter_val = (stable_jitter - 0.5) * 24
    bedtime_threshold_offset = _logit(cutoff) * _SLEEP_SIGMOID_MINUTES
    effective_bedtime_at = bedtime_at - int((energy_shift + chat_delay + jitter_val + bedtime_threshold_offset) * _MINUTE_MS)

    planned_sleep_minutes = (wake_at - bedtime_at) / _MINUTE_MS
    if sleep_started_at is None:
        projected_sleep_minutes = planned_sleep_minutes
    else:
        projected_sleep_minutes = max(0.0, (wake_at - sleep_started_at) / _MINUTE_MS)
    missing_sleep_minutes = max(0.0, planned_sleep_minutes - projected_sleep_minutes)
    sleep_debt_delay_minutes = missing_sleep_minutes * _LATE_SLEEP_COMPENSATION

    natural_wake_target_at = wake_at + int((sleep_debt_delay_minutes + jitter_val) * _MINUTE_MS)
    wake_threshold_offset = _logit(effective_wake_hysteresis) * _SLEEP_SIGMOID_MINUTES
    effective_wake_at = natural_wake_target_at + int(wake_threshold_offset * _MINUTE_MS)

    minutes_from_bedtime = (now - bedtime_at) / _MINUTE_MS
    bedtime_rise = _sigmoid((now - effective_bedtime_at) / _MINUTE_MS / _SLEEP_SIGMOID_MINUTES)
    wake_fall = _sigmoid((effective_wake_at - now) / _MINUTE_MS / _SLEEP_SIGMOID_MINUTES)
    probability = bedtime_rise * wake_fall

    asleep = (probability > effective_wake_hysteresis) if previously_asleep else (probability >= cutoff)

    return SleepEvaluation(
        asleep=asleep,
        drowsy=not asleep and probability >= _DROWSY_PROBABILITY,
        just_woke=False,
        probability=probability,
        cutoff=cutoff,
        minutes_from_bedtime=minutes_from_bedtime,
        effective_wake_at=effective_wake_at,
        natural_wake_target_at=natural_wake_target_at,
        sleep_debt_delay_minutes=sleep_debt_delay_minutes,
    )


class SleepStateController:
    def __init__(
        self,
        input_source: Callable[[int], SleepInputs],
        wake_grace_ms: int = WAKE_GRACE_MS,
        state_store: Any = None,
        forced_asleep: Callable[[], bool | None] | None = None,
    ) -> None:
        self._input_source = input_source
        self._wake_grace_ms = wake_grace_ms
        self._state_store = state_store
        self._forced_asleep = forced_asleep
        self._woken_until = 0
        self._sleeping = False
        self._sleep_started_at: int | None = None
        self._woke_at: int | None = None
        self._restored = False

    def current(self, now: int | None = None) -> SleepState:
        now = now if now is not None else current_time()
        self._restore()
        forced = self._forced_asleep() if self._forced_asleep else None
        if forced is not None:
            self._sleeping = forced
            return SleepState(asleep=forced, drowsy=False, just_woke=False, probability=1.0 if forced else 0.0)
        inputs = self._input_source(now)
        evaluated = evaluate_sleep(inputs, now, self._sleeping, sleep_started_at=self._sleep_started_at)
        if now < self._woken_until:
            self._change_state(False, now)
            just_woke = self._is_just_woke(now, evaluated.natural_wake_target_at)
            return SleepState(asleep=False, drowsy=False, just_woke=just_woke, probability=evaluated.probability)
        self._change_state(evaluated.asleep, now)
        just_woke = not evaluated.asleep and self._is_just_woke(now, evaluated.natural_wake_target_at)
        return SleepState(asleep=evaluated.asleep, drowsy=evaluated.drowsy,
                          just_woke=just_woke, probability=evaluated.probability)

    def inspect(self, now: int | None = None) -> SleepEvaluation:
        now = now if now is not None else current_time()
        self._restore()
        inputs = self._input_source(now)
        evaluated = evaluate_sleep(inputs, now, self._sleeping, sleep_started_at=self._sleep_started_at)
        forced = self._forced_asleep() if self._forced_asleep else None
        if forced is not None:
            return SleepEvaluation(asleep=forced, drowsy=False, just_woke=False,
                                   probability=1.0 if forced else 0.0)
        return SleepEvaluation(
            **{**evaluated.__dict__, 'just_woke': not evaluated.asleep and self._is_just_woke(now, evaluated.natural_wake_target_at)}
        )

    def wake(self, now: int | None = None) -> SleepState:
        now = now if now is not None else current_time()
        self._woken_until = now + self._wake_grace_ms
        self._change_state(False, now)
        return self.current(now)

    def _change_state(self, asleep: bool, now: int) -> None:
        if asleep and not self._sleeping:
            self._sleep_started_at = now
            self._persist()
        elif not asleep and self._sleeping:
            self._woke_at = now
            self._sleep_started_at = None
            self._persist()
        self._sleeping = asleep

    def _is_just_woke(self, now: int, natural_wake_target_at: int) -> bool:
        if self._woke_at is None:
            return False
        return (now - self._woke_at) < WAKE_TRANSITION_MS

    def _persist(self) -> None:
        if self._state_store:
            self._state_store.write_json(_SLEEP_RUNTIME_KEY, {
                'sleeping': self._sleeping,
                'sleepStartedAt': self._sleep_started_at,
            })

    def _restore(self) -> None:
        if self._restored or not self._state_store:
            return
        self._restored = True
        saved = self._state_store.read_json(_SLEEP_RUNTIME_KEY, None)
        if saved and isinstance(saved, dict):
            self._sleeping = bool(saved.get('sleeping', False))
            self._sleep_started_at = saved.get('sleepStartedAt')
