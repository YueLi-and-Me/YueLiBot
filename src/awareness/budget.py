"""
主动打扰的预算与节流。直接移植自 src/core/awareness/budget.ts。

所有默认值偏保守：少说一句只是平淡，多说一句会让人想卸载。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

DAILY_BUDGET = 5
SCENE_RESERVED_EVENT_SLOTS = 2


@dataclass
class ProactiveState:
    day_key: str
    used: int = 0
    last_at: int = 0
    ignored: int = 0


@dataclass
class InterruptContext:
    now: int
    silent: bool = False
    asleep: bool = False
    visible: bool = True
    responded_since_last: bool = True
    priority: Literal['normal', 'high'] | None = None


@dataclass
class Decision:
    allow: bool
    reason: str | None = None


def day_key_of(now: int) -> str:
    d = datetime.fromtimestamp(now / 1000)
    return f'{d.year}-{d.month}-{d.day}'


def initial_state(now: int) -> ProactiveState:
    return ProactiveState(day_key=day_key_of(now))


def _rollover(state: ProactiveState, now: int) -> ProactiveState:
    key = day_key_of(now)
    if key == state.day_key:
        return state
    return ProactiveState(day_key=key, used=0, last_at=state.last_at, ignored=state.ignored)


def decide(state: ProactiveState, ctx: InterruptContext) -> Decision:
    if ctx.silent:
        return Decision(allow=False, reason='silent')
    if ctx.asleep:
        return Decision(allow=False, reason='asleep')
    if not ctx.visible:
        return Decision(allow=False, reason='hidden')
    s = _rollover(state, ctx.now)
    if ctx.priority == 'high':
        return Decision(allow=True)
    if s.used >= DAILY_BUDGET:
        return Decision(allow=False, reason='budget')
    return Decision(allow=True)


def decide_scene(state: ProactiveState, ctx: InterruptContext) -> Decision:
    base = decide(state, ctx)
    if not base.allow or ctx.priority == 'high':
        return base
    s = _rollover(state, ctx.now)
    if s.used >= DAILY_BUDGET - SCENE_RESERVED_EVENT_SLOTS:
        return Decision(allow=False, reason='scene-reserve')
    return base


def after_speak(state: ProactiveState, ctx: InterruptContext) -> ProactiveState:
    s = _rollover(state, ctx.now)
    return ProactiveState(
        day_key=s.day_key,
        used=s.used if ctx.priority == 'high' else s.used + 1,
        last_at=ctx.now,
        ignored=0 if ctx.responded_since_last else s.ignored + 1,
    )


def after_user_spoke(state: ProactiveState) -> ProactiveState:
    if state.ignored == 0:
        return state
    return ProactiveState(day_key=state.day_key, used=state.used,
                          last_at=state.last_at, ignored=0)


def describe_budget(state: ProactiveState, now: int) -> dict:
    s = _rollover(state, now)
    return {
        'day_key': s.day_key,
        'used': s.used,
        'remaining': max(0, DAILY_BUDGET - s.used),
        'ignored': s.ignored,
    }
