"""SQLite 管线事件账本。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import json
import sqlite3
import threading

from src.common.clock import now as current_time
from src.common.db.schema import EVENTS_DDL
from src.observe.stages import label_for


_CLEANUP_EVERY = 500


@dataclass(frozen=True)
class EventPage:
    events: List[Dict[str, Any]]
    truncated: bool
    from_seq: int | None


class EventStore:
    """独立的事件账本连接。"""

    def __init__(self) -> None:
        self._connection: sqlite3.Connection | None = None
        self._retention_count = 20_000
        self._retention_hours = 72
        self._writes = 0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self._connection is not None

    def configure(
        self,
        db_path: str | Path,
        *,
        retention_count: int = 20_000,
        retention_hours: int = 72,
    ) -> None:
        if retention_count < 1:
            raise ValueError("事件保留条数必须大于 0")
        if retention_hours < 0:
            raise ValueError("事件保留时长不能小于 0")
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            connection = sqlite3.connect(str(db_path), check_same_thread=False)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.execute("PRAGMA busy_timeout = 3000")
                connection.executescript(EVENTS_DDL)
                connection.commit()
            except Exception:
                connection.close()
                raise
            self._connection = connection
            self._retention_count = retention_count
            self._retention_hours = retention_hours
            self._writes = 0

    def append(
        self,
        kind: str,
        stage: str,
        stream_id: int | None,
        turn_id: int | None,
        payload: Dict[str, Any],
        *,
        at: int | None = None,
    ) -> Dict[str, Any]:
        event_at = current_time() if at is None else at
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock:
            connection = self._require_connection()
            cursor = connection.execute(
                """
                INSERT INTO pipeline_events (at, stream_id, turn_id, stage, kind, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_at, stream_id, turn_id, stage, kind, encoded),
            )
            connection.commit()
            seq = int(cursor.lastrowid)
            self._writes += 1
            if self._writes % _CLEANUP_EVERY == 0:
                self._cleanup(connection, event_at)
        return self._flatten(seq, event_at, kind, stage, stream_id, turn_id, payload)

    def since(self, seq: int, limit: int = 1_000) -> EventPage:
        if seq < 0:
            raise ValueError("事件游标不能小于 0")
        if limit < 1:
            raise ValueError("事件读取上限必须大于 0")
        with self._lock:
            connection = self._require_connection()
            rows = connection.execute(
                """
                SELECT seq, at, stream_id, turn_id, stage, kind, payload
                FROM pipeline_events
                WHERE seq > ?
                ORDER BY seq DESC
                LIMIT ?
                """,
                (seq, limit + 1),
            ).fetchall()
        truncated = len(rows) > limit
        selected = list(reversed(rows[:limit]))
        events = [self._row_to_event(row) for row in selected]
        return EventPage(
            events=events,
            truncated=truncated,
            from_seq=events[0]["seq"] if events else None,
        )

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._writes = 0

    def clear(self) -> None:
        with self._lock:
            if self._connection is None:
                return
            self._connection.execute("DELETE FROM pipeline_events")
            self._connection.commit()
            self._writes = 0

    def _cleanup(self, connection: sqlite3.Connection, now: int) -> None:
        cutoff = now - self._retention_hours * 60 * 60 * 1_000
        connection.execute(
            """
            DELETE FROM pipeline_events
            WHERE at <= ?
               OR seq <= COALESCE((
                    SELECT seq
                    FROM pipeline_events
                    ORDER BY seq DESC
                    LIMIT 1 OFFSET ?
               ), 0)
            """,
            (cutoff, self._retention_count),
        )
        connection.commit()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("事件账本尚未配置")
        return self._connection

    @staticmethod
    def _flatten(
        seq: int,
        at: int,
        kind: str,
        stage: str,
        stream_id: int | None,
        turn_id: int | None,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            **payload,
            "seq": seq,
            "at": at,
            "kind": kind,
            "stage": stage,
            "stageLabel": label_for(stage),
            "streamId": stream_id,
            "turnId": turn_id,
        }

    def _row_to_event(self, row: sqlite3.Row) -> Dict[str, Any]:
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict):
            raise ValueError(f"事件 {row['seq']} 的 payload 不是对象")
        return self._flatten(
            int(row["seq"]),
            int(row["at"]),
            str(row["kind"]),
            str(row["stage"]),
            int(row["stream_id"]) if row["stream_id"] is not None else None,
            int(row["turn_id"]) if row["turn_id"] is not None else None,
            payload,
        )


event_store = EventStore()


def configure(
    db_path: str | Path,
    *,
    retention_count: int = 20_000,
    retention_hours: int = 72,
) -> None:
    event_store.configure(
        db_path,
        retention_count=retention_count,
        retention_hours=retention_hours,
    )


def since(seq: int, limit: int = 1_000) -> EventPage:
    return event_store.since(seq, limit)


def close() -> None:
    event_store.close()
