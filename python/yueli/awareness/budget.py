"""
主动打扰的预算与节流。直接移植自 src/core/awareness/budget.ts。

所有默认值偏保守：少说一句只是平淡，多说一句会让人想卸载。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Optional

DAILY_BUDGET = 5
COOLDOWN_MS = 30 * 60_000
IGNORE_LIMIT = 3
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
    from datetime import datetime
    d = datetime.fromtimestamp(now / 1000)
    return f'{d.year}-{d.month}-{d.day}'


def initial_state(now: int) -> ProactiveState:
    return ProactiveState(day_key=day_key_of(now))


def _rollover(state: ProactiveState, now: int) -> ProactiveState:
    key = day_key_of(now)
    if key == state.day_key:
        return state
    return ProactiveState(day_key=key, used=0, last_at=state.last_at, ignored=state.ignored)


def cooldown_for(ignored: int) -> int:
    if ignored < IGNORE_LIMIT:
        return COOLDOWN_MS
    factor = min(8, 2 ** (ignored - IGNORE_LIMIT + 1))
    return min(4 * 3_600_000, COOLDOWN_MS * factor)


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
    ignored = 0 if ctx.responded_since_last else s.ignored
    cooldown = cooldown_for(ignored)
    if s.last_at and ctx.now - s.last_at < cooldown:
        return Decision(allow=False, reason='cooldown')
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
    cooldown = cooldown_for(s.ignored)
    next_allowed = 0
    if s.last_at:
        next_allowed = max(0, round((s.last_at + cooldown - now) / 60_000))
    return {
        'day_key': s.day_key,
        'used': s.used,
        'remaining': max(0, DAILY_BUDGET - s.used),
        'ignored': s.ignored,
        'cooldown_minutes': round(cooldown / 60_000),
        'next_allowed_in_minutes': next_allowed,
    }
