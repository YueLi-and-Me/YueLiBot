"""视觉瞥视本地状态机。直接移植自 src/core/awareness/lookState.ts。"""

from __future__ import annotations

from collections import deque
from typing import Any

from .classify import VisionContext
from .look import LookDecision, LookReason, should_look, LookInput


# 每个场景保留几张关键帧。多送几帧是「看懂动态」的前提——模型本身只会看
# 单图，帧间关系要靠我们把序列摆给它。参考妹居物语走的那条路：RTC 服务端
# 做关键帧抽取，再把帧序列交给模型做时序分析，而不是指望模型能读视频流。
MAX_KEYFRAMES = 4


class VisionLookState:
    def __init__(self) -> None:
        self._context: VisionContext | None = None
        self._context_since = 0
        self._last_looked_at = 0
        self._last_calls: dict[VisionContext, int] = {}
        self._frames: dict[VisionContext, Any] = {}
        self._keyframes: dict[VisionContext, deque] = {}

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

    def push_keyframe(self, context: VisionContext, frame: Any, limit: int = MAX_KEYFRAMES) -> None:
        """把一帧收进该场景的关键帧序列，最旧的自动挤出去。

        只有被判定为「有变化」的帧才该调这个——静止画面重复入列，等于用
        N 张一模一样的图去问模型发生了什么变化。
        """
        buffer = self._keyframes.get(context)
        if buffer is None or buffer.maxlen != max(1, limit):
            buffer = deque(buffer or (), maxlen=max(1, limit))
            self._keyframes[context] = buffer
        buffer.append(frame)

    def keyframes(self, context: VisionContext) -> list[Any]:
        """该场景的关键帧，按时间从旧到新。"""
        return list(self._keyframes.get(context, ()))

    def clear_keyframes(self, context: VisionContext) -> None:
        self._keyframes.pop(context, None)

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
