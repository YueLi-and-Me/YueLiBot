"""主动感知重做中兴趣值层的验收断言。"""

from __future__ import annotations

import pytest

from src.core.awareness.budget import InterruptContext, after_speak, decide, initial_state as initial_budget_state
from src.core.awareness.interest import factors_for, grow, initial_state


T0 = 1_760_000_000_000
MINUTE_MS = 60_000


def _factors(*, activity: str = 'idle', intensity: str = 'light', favor: float = 50,
             energy: float = 50, ignored: int = 0, absence_hours: float = 0):
    return factors_for(activity, intensity, favor, energy, ignored, absence_hours)


def test_interest_uses_elapsed_time_not_tick_count() -> None:
    factors = _factors(absence_hours=4)
    once = grow(initial_state(T0), factors, T0 + 10 * MINUTE_MS)
    split = initial_state(T0)
    for minute in range(1, 11):
        split = grow(split, factors, T0 + minute * MINUTE_MS)
    assert split.value == pytest.approx(once.value)


def test_budget_no_longer_has_fixed_cooldown() -> None:
    ctx = InterruptContext(now=T0)
    state = after_speak(initial_budget_state(T0), ctx)
    assert decide(state, InterruptContext(now=T0 + MINUTE_MS)).allow is True


def test_away_interest_is_far_lower_than_light_input() -> None:
    away = _factors(intensity='away').rate_per_minute
    light = _factors(intensity='light').rate_per_minute
    assert away < light / 10


def test_ignored_and_absence_multipliers_are_continuous_and_bounded() -> None:
    assert _factors(ignored=0).rate_per_minute > _factors(ignored=2).rate_per_minute > _factors(ignored=4).rate_per_minute
    fresh = _factors(absence_hours=0).rate_per_minute
    four_hours = _factors(absence_hours=4).rate_per_minute
    one_day = _factors(absence_hours=24).rate_per_minute
    three_days = _factors(absence_hours=72).rate_per_minute
    assert fresh < four_hours < one_day
    assert three_days / one_day < 1.2


def test_low_persona_axes_slow_interest_without_stopping_it() -> None:
    assert _factors(favor=5).rate_per_minute > 0
    assert _factors(energy=5).rate_per_minute > 0
