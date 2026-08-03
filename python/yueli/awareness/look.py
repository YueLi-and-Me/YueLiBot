"""瞥视决策逻辑。直接移植自 src/core/awareness/look.ts。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .classify import VisionContext

LOOK_COOLDOWN_MS = 90_000
FRAME_CHANGE_THRESHOLD = 0.18
IDLE_GLANCE_AFTER_MS = 3 * 60_000
IDLE_GLANCE_INTERVAL_MS = 10 * 60_000

LookReason = Literal['cooldown', 'frame-change', 'idle-glance', 'folder-switch', 'no-change']


@dataclass
class LookInput:
    context: VisionContext
    last_call_at: int
    context_since: int
    delta: float
    window_changed: bool
    now: int


@dataclass
class LookDecision:
    look: bool
    reason: LookReason


def within_look_cooldown(now: int, last_look_at: int) -> bool:
    return now - last_look_at < LOOK_COOLDOWN_MS


def should_look(inp: LookInput) -> LookDecision:
    if inp.context == 'game-folder':
        return LookDecision(look=inp.window_changed, reason='folder-switch' if inp.window_changed else 'no-change')
    if inp.delta >= FRAME_CHANGE_THRESHOLD:
        return LookDecision(look=True, reason='frame-change')
    stayed_long_enough = inp.now - inp.context_since >= IDLE_GLANCE_AFTER_MS
    called_long_enough_ago = inp.now - inp.last_call_at >= IDLE_GLANCE_INTERVAL_MS
    if stayed_long_enough and called_long_enough_ago:
        return LookDecision(look=True, reason='idle-glance')
    return LookDecision(look=False, reason='no-change')
