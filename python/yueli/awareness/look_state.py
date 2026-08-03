"""视觉瞥视本地状态机。直接移植自 src/core/awareness/lookState.ts。"""

from __future__ import annotations

from typing import Any

from .classify import VisionContext
from .look import LookDecision, LookReason, should_look, LookInput


class VisionLookState:
    def __init__(self) -> None:
        self._context: VisionContext | None = None
        self._context_since = 0
        self._last_looked_at = 0
        self._last_calls: dict[VisionContext, int] = {}
        self._frames: dict[VisionContext, Any] = {}

    @property
    def last_look_at(self) -> int:
        return self._last_looked_at

    def enter(self, context: VisionContext | None, now: int) -> None:
        if context == self._context:
            return
        self._context = context
        self._context_since = now

    def swap_frame(self, context: VisionContext, frame: Any) -> Any | None:
        previous = self._frames.get(context)
        self._frames[context] = frame
        return previous

    def evaluate(self, context: VisionContext, now: int, delta: float, window_changed: bool) -> LookDecision:
        return should_look(LookInput(
            context=context,
            now=now,
            last_call_at=self._last_calls.get(context, 0),
            context_since=self._context_since,
            delta=delta,
            window_changed=window_changed,
        ))

    def note_look(self, now: int) -> None:
        self._last_looked_at = now

    def note_call(self, context: VisionContext, now: int) -> None:
        self._last_calls[context] = now
