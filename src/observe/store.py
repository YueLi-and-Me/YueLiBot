"""提供 SQLite 管线事件账本的独立连接和游标分页读取。

`EventStore` 使用单独连接写入事件，按数量和时间周期清理旧记录；实时事件广播
由 `src.observe.events` 负责，读取方可先用游标回放再订阅实时流。
"""

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
    """表示一次事件游标查询结果。

    :ivar events: 按 seq 正序排列的事件列表。
    :ivar truncated: 是否还有超出本次 limit 的更早/更晚结果。
    :ivar from_seq: 返回页第一条事件的 seq；无事件时为 `None`。
    """

    events: List[Dict[str, Any]]
    truncated: bool
    from_seq: int | None


class EventStore:
    """管理独立 SQLite 连接上的管线事件账本。"""

    def __init__(self) -> None:
        """创建未配置数据库路径的事件存储。

        :side_effects: 初始化连接引用、保留策略、写入计数和线程锁，不打开文件。
        """
        self._connection: sqlite3.Connection | None = None
        self._retention_count = 20_000
        self._retention_hours = 72
        self._writes = 0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        """返回事件账本是否已配置数据库连接。

        :return: 已调用 :meth:`configure` 且连接仍存在时为 `True`。
        :side_effects: 不执行 I/O。
        """
        return self._connection is not None

    def configure(
        self,
        db_path: str | Path,
        *,
        retention_count: int = 20_000,
        retention_hours: int = 72,
    ) -> None:
        """打开或替换事件账本连接并创建事件表。

        :param db_path: SQLite 数据库路径。
        :param retention_count: 最多保留的事件数量，默认值为 20000。
        :param retention_hours: 事件最大保留时长，单位小时，默认值为 72；0 表示禁用按时间清理。
        :raises ValueError: 保留数量小于 1 或保留时长小于 0。
        :raises (OSError, sqlite3.Error): 数据库打开、DDL 或提交失败。
        :side_effects: 关闭旧连接，打开新连接，创建表并重置写入计数。
        """
        # 先校验保留策略，避免替换当前可用连接后才发现配置无效。
        if retention_count < 1:
            raise ValueError("事件保留条数必须大于 0")
        if retention_hours < 0:
            raise ValueError("事件保留时长不能小于 0")
        with self._lock:
            # 连接替换和新表初始化必须在同一锁内完成，避免写线程看到半初始化状态。
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
                # 初始化失败时关闭临时连接，保持旧连接已关闭且新连接不泄漏。
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
        """写入一条管线事件并返回扁平化事件字典。

        :param kind: 事件类型。
        :param stage: 产生事件时的阶段 ID。
        :param stream_id: 关联 stream ID，可以为 `None`。
        :param turn_id: 关联 turn ID，可以为 `None`。
        :param payload: 事件业务字段。
        :param at: 可选事件时间戳；省略时读取当前毫秒时钟。
        :return: 含 seq、时间、阶段、stream 和 payload 字段的事件字典。
        :raises RuntimeError: 账本尚未配置。
        :raises (TypeError, sqlite3.Error): payload 不可序列化或写入失败。
        :side_effects: 插入事件并提交；每 500 次写入按保留策略清理旧记录。
        """
        # 先序列化 payload，失败时不进入事务，避免写入不可重建的半条事件。
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
                # 清理按批次触发，避免每条事件都执行全量保留检查。
                self._cleanup(connection, event_at)
        return self._flatten(seq, event_at, kind, stage, stream_id, turn_id, payload)

    def since(self, seq: int, limit: int = 1_000) -> EventPage:
        """读取游标之后的事件并按正序返回分页结果。

        :param seq: 上次已确认的最大 seq，必须大于等于 0。
        :param limit: 最多返回的事件数，默认值为 1000，必须大于 0。
        :return: `EventPage`；结果按 seq 正序排列。
        :raises ValueError: seq 小于 0 或 limit 小于 1。
        :raises RuntimeError: 账本尚未配置。
        :side_effects: 只读事件表。
        """
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

    def current_stages(self, scan_limit: int = 50) -> List[Dict[str, Any]]:
        """从事件账本恢复每条 stream 的当前阶段及连续停留时长。

        :param scan_limit: 每条 stream 最多向前扫描的阶段事件数，默认值为 50。
        :return: 按最新阶段事件时间倒序排列的阶段快照。
        :raises ValueError: 扫描上限小于 1。
        :raises RuntimeError: 账本尚未配置。
        :side_effects: 只读 ``pipeline_events``；不修改事件或业务状态。
        """
        if scan_limit < 1:
            raise ValueError("阶段扫描上限必须大于 0")
        now = current_time()
        with self._lock:
            connection = self._require_connection()
            latest_rows = connection.execute(
                """
                SELECT event.seq, event.at, event.stream_id, event.turn_id,
                       event.stage, event.payload
                FROM pipeline_events AS event
                INNER JOIN (
                    SELECT stream_id, MAX(seq) AS seq
                    FROM pipeline_events
                    WHERE kind = 'stage' AND stream_id IS NOT NULL
                    GROUP BY stream_id
                ) AS latest ON latest.seq = event.seq
                ORDER BY event.at DESC, event.seq DESC
                """
            ).fetchall()
            snapshots: List[Dict[str, Any]] = []
            for latest in latest_rows:
                history = connection.execute(
                    """
                    SELECT at, stage
                    FROM pipeline_events
                    WHERE kind = 'stage' AND stream_id = ? AND seq <= ?
                    ORDER BY seq DESC
                    LIMIT ?
                    """,
                    (latest["stream_id"], latest["seq"], scan_limit),
                ).fetchall()
                started_at = int(latest["at"])
                for row in history:
                    if str(row["stage"]) != str(latest["stage"]):
                        break
                    started_at = int(row["at"])
                payload = json.loads(latest["payload"])
                if not isinstance(payload, dict):
                    raise ValueError(f"事件 {latest['seq']} 的 payload 不是对象")
                snapshots.append({
                    "streamId": int(latest["stream_id"]),
                    "streamName": str(payload.get("streamName", "")),
                    "stage": str(latest["stage"]),
                    "stageLabel": label_for(str(latest["stage"])),
                    "detail": str(payload.get("detail", "")),
                    "turnId": (
                        int(latest["turn_id"])
                        if latest["turn_id"] is not None
                        else None
                    ),
                    "stageElapsedMs": max(0, now - started_at),
                    "updatedAt": int(latest["at"]),
                })
        return snapshots

    def close(self) -> None:
        """关闭事件账本连接并重置写入计数。

        :side_effects: 关闭 SQLite 连接；重复调用安全。
        """
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._writes = 0

    def clear(self) -> None:
        """删除当前账本中的全部事件。

        :side_effects: 清空 pipeline_events 表并提交事务；未配置连接时无操作。
        :raises sqlite3.Error: 删除或提交失败。
        """
        with self._lock:
            if self._connection is None:
                return
            self._connection.execute("DELETE FROM pipeline_events")
            self._connection.commit()
            self._writes = 0

    def _cleanup(self, connection: sqlite3.Connection, now: int) -> None:
        """按时间和数量保留策略删除旧事件。

        :param connection: 当前事件账本连接。
        :param now: 当前事件时间戳。
        :side_effects: 删除超出时长或数量上限的事件并提交事务。
        :raises sqlite3.Error: 清理 SQL 或提交失败。
        """
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
        """返回已配置的 SQLite 连接。

        :return: 当前账本连接。
        :raises RuntimeError: 尚未调用 :meth:`configure` 或连接已关闭。
        :side_effects: 不执行 I/O。
        """
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
        """把数据库字段和业务 payload 合并为对外事件结构。

        :param seq: 数据库事件序号。
        :param at: 事件时间戳。
        :param kind: 事件类型。
        :param stage: 阶段 ID。
        :param stream_id: stream ID，可以为 `None`。
        :param turn_id: turn ID，可以为 `None`。
        :param payload: 业务字段字典。
        :return: 合并后的新字典。
        :side_effects: 不修改 payload。
        """
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
        """把 SQLite 行解析为扁平化事件字典。

        :param row: 包含事件固定列和 JSON payload 的 SQLite 行。
        :return: 通过 :meth:`_flatten` 生成的事件字典。
        :raises json.JSONDecodeError: payload 不是合法 JSON。
        :raises ValueError: payload 解码后不是对象。
        :side_effects: 不修改数据库行。
        """
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
    """配置模块级事件账本。

    :param db_path: SQLite 数据库路径。
    :param retention_count: 最大保留事件数，默认值为 20000。
    :param retention_hours: 最大保留时长，默认值为 72 小时。
    :side_effects: 委托全局 `event_store` 替换连接并创建表。
    """
    event_store.configure(
        db_path,
        retention_count=retention_count,
        retention_hours=retention_hours,
    )


def since(seq: int, limit: int = 1_000) -> EventPage:
    """读取模块级事件账本的游标分页。

    :param seq: 上次已确认的最大 seq。
    :param limit: 最大事件数量，默认值为 1000。
    :return: 事件分页结果。
    :raises ValueError: 游标或上限不合法。
    :raises RuntimeError: 模块级账本尚未配置。
    """
    return event_store.since(seq, limit)


def current_stages(scan_limit: int = 50) -> List[Dict[str, Any]]:
    """读取模块级事件账本中的当前阶段快照。

    :param scan_limit: 每条 stream 最多扫描的阶段事件数量。
    :return: 按更新时间倒序排列的阶段快照。
    :raises ValueError: 扫描上限不合法。
    :raises RuntimeError: 模块级账本尚未配置。
    """
    return event_store.current_stages(scan_limit)


def close() -> None:
    """关闭模块级事件账本连接。

    :side_effects: 委托全局 `event_store` 关闭 SQLite 连接。
    """
    event_store.close()
