"""每条 stream 当前所在的管线阶段。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import threading

from src.common.clock import now as current_time
from src.observe.stages import Stage


@dataclass
class _Entry:
    stream_id: int
    stream_name: str
    stage: Stage
    detail: str
    turn_id: int | None
    stage_started_at: int
    updated_at: int


class StageBoard:
    """维护各流的当前阶段。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[int, _Entry] = {}

    def _update(
        self,
        stream_id: int,
        stream_name: str,
        stage: Stage,
        detail: str = "",
        turn_id: int | None = None,
    ) -> None:
        now = current_time()
        with self._lock:
            current = self._entries.get(stream_id)
            started_at = (
                now
                if current is None or current.stage.id != stage.id
                else current.stage_started_at
            )
            self._entries[stream_id] = _Entry(
                stream_id=stream_id,
                stream_name=stream_name,
                stage=stage,
                detail=detail,
                turn_id=turn_id,
                stage_started_at=started_at,
                updated_at=now,
            )

    def snapshot(self) -> List[dict]:
        now = current_time()
        with self._lock:
            entries = list(self._entries.values())
        entries.sort(key=lambda entry: entry.updated_at, reverse=True)
        return [
            {
                "streamId": entry.stream_id,
                "streamName": entry.stream_name,
                "stage": entry.stage.id,
                "stageLabel": entry.stage.label,
                "detail": entry.detail,
                "turnId": entry.turn_id,
                "stageElapsedMs": now - entry.stage_started_at,
                "updatedAt": entry.updated_at,
            }
            for entry in entries
        ]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


board = StageBoard()
