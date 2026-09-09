"""提供 SQLite 管线事件账本的独立连接和游标分页读取。

`EventStore` 使用单独连接写入事件，按数量和时间周期清理旧记录；实时事件广播
由 `src.core.observe.events` 负责，读取方可先用游标回放再订阅实时流。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import json
import sqlite3
import threading

from src.core.runtime.clock import now as current_time
from src.core.db.schema import EVENTS_DDL
from src.core.observe.stages import label_for


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


@dataclass(frozen=True)
class EventSearchPage:
    """表示一次历史事件倒序检索结果。

    :ivar events: 按 seq 倒序排列的事件列表。
    :ivar next_cursor: 下一页应传入的排他 seq 游标；没有更多结果时为 ``None``。
    """

    events: List[Dict[str, Any]]
    next_cursor: int | None


class EventStore:
    """管理独立 SQLite 连接上的管线事件账本。"""

    def __init__(self) -> None:
        """创建未配置数据库路径的事件存储。

        副作用：初始化连接引用、保留策略、写入计数和线程锁，不打开文件。
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
        副作用：不执行 I/O。
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
        副作用：关闭旧连接，打开新连接，创建表并重置写入计数。
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
        副作用：插入事件并提交；每 500 次写入按保留策略清理旧记录。
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
        副作用：只读事件表。
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
        副作用：只读 ``pipeline_events``；不修改事件或业务状态。
        """
        if scan_limit < 1:
            raise ValueError("阶段扫描上限必须大于 0")
        now = current_time()
        with self._lock:
            connection = self._require_connection()
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT seq, at, stream_id, turn_id, stage, payload,
                           ROW_NUMBER() OVER (
                               PARTITION BY stream_id ORDER BY seq DESC
                           ) AS position
                    FROM pipeline_events
                    WHERE kind = 'stage' AND stream_id IS NOT NULL
                )
                SELECT seq, at, stream_id, turn_id, stage, payload, position
                FROM ranked
                WHERE position <= ?
                ORDER BY stream_id, position
                """,
                (scan_limit,),
            ).fetchall()
        histories: Dict[int, List[sqlite3.Row]] = {}
        for row in rows:
            histories.setdefault(int(row["stream_id"]), []).append(row)
        snapshots: List[Dict[str, Any]] = []
        for history in histories.values():
            latest = history[0]
            started_at = int(latest["at"])
            started_at_truncated = True
            for previous in history[1:]:
                if str(previous["stage"]) != str(latest["stage"]):
                    started_at_truncated = False
                    break
                started_at = int(previous["at"])
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
                "stageStartedAtTruncated": started_at_truncated,
                "updatedAt": int(latest["at"]),
            })
        return sorted(
            snapshots,
            key=lambda snapshot: snapshot["updatedAt"],
            reverse=True,
        )

    def search(
        self,
        *,
        stream_id: int | None = None,
        turn_id: int | None = None,
        kinds: List[str] | None = None,
        since_at: int | None = None,
        until_at: int | None = None,
        limit: int = 200,
        cursor: int | None = None,
    ) -> EventSearchPage:
        """按会话、轮次、类型和时间范围倒序检索历史事件。

        :param stream_id: 可选 stream ID。
        :param turn_id: 可选轮次 ID。
        :param kinds: 可选事件类型列表，列表内按多选匹配。
        :param since_at: 可选左闭毫秒时间边界。
        :param until_at: 可选右开毫秒时间边界。
        :param limit: 返回条数，范围为 1 到 1000。
        :param cursor: 可选排他 seq 游标，只返回 seq 更小的记录。
        :return: 倒序事件和下一页游标。
        :raises ValueError: 参数范围或时间区间不合法。
        :raises RuntimeError: 账本尚未配置。
        副作用：只读事件表。
        """
        if limit < 1 or limit > 1_000:
            raise ValueError("事件检索上限必须在 1 到 1000 之间")
        if cursor is not None and cursor < 1:
            raise ValueError("事件检索游标必须大于 0")
        if since_at is not None and until_at is not None and since_at >= until_at:
            raise ValueError("事件检索开始时间必须早于结束时间")
        selected_kinds = [kind for kind in (kinds or []) if kind]
        clauses: List[str] = []
        parameters: List[Any] = []
        if stream_id is not None:
            clauses.append("stream_id = ?")
            parameters.append(stream_id)
        if turn_id is not None:
            clauses.append("turn_id = ?")
            parameters.append(turn_id)
        if selected_kinds:
            placeholders = ", ".join("?" for _ in selected_kinds)
            clauses.append(f"kind IN ({placeholders})")
            parameters.extend(selected_kinds)
        if since_at is not None:
            clauses.append("at >= ?")
            parameters.append(since_at)
        if until_at is not None:
            clauses.append("at < ?")
            parameters.append(until_at)
        if cursor is not None:
            clauses.append("seq < ?")
            parameters.append(cursor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit + 1)
        with self._lock:
            connection = self._require_connection()
            rows = connection.execute(
                f"""
                SELECT seq, at, stream_id, turn_id, stage, kind, payload
                FROM pipeline_events
                {where}
                ORDER BY seq DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        truncated = len(rows) > limit
        events = [self._row_to_event(row) for row in rows[:limit]]
        return EventSearchPage(
            events=events,
            next_cursor=events[-1]["seq"] if truncated and events else None,
        )

    def event(self, seq: int) -> Dict[str, Any] | None:
        """按主键读取一条持久化事件。"""
        if seq < 1:
            raise ValueError("事件序号必须大于 0")
        with self._lock:
            connection = self._require_connection()
            row = connection.execute(
                """
                SELECT seq, at, stream_id, turn_id, stage, kind, payload
                FROM pipeline_events
                WHERE seq = ?
                """,
                (seq,),
            ).fetchone()
        return self._row_to_event(row) if row is not None else None

    def assistant_message_turn_id(self, stream_id: int, message_id: int) -> int | None:
        """由明确的消息关联事件查回合号；旧历史或已清理账本返回未知，不猜时间戳。"""
        with self._lock:
            connection = self._require_connection()
            row = connection.execute(
                """SELECT turn_id FROM pipeline_events
                   WHERE stream_id = ? AND kind = 'assistant_reply_recorded'
                     AND json_extract(payload, '$.messageId') = ?
                   ORDER BY seq DESC LIMIT 1""",
                (stream_id, message_id),
            ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None

    def max_turn_id(self) -> int:
        """返回账本中已出现过的最大回合 ID。

        供 :class:`~src.core.services.chat.ChatService` 在启动时给回合计数器播种。
        回合 ID 由进程内计数器分配，而账本跨重启持久化——不播种就会每次启动从 1
        重新发号，与上次运行的回合撞号。真机实测：``turn_id = 33`` 曾同时装着 4 次
        不同启动的对话、横跨 31 小时，WebUI 按 turnId 聚合时把它们并成一张卡。

        账本按 ``retention_count`` / ``retention_hours`` 清理旧事件，因此这里取到的
        是留存事件中的最大值——被清理的回合已不在账本里，不构成碰撞源，
        所以这个口径是充分的。

        :return: 最大 ``turn_id``；账本为空或尚未配置时返回 ``0``。账本没配置就没有
            可碰撞的历史回合，从 0 起算是正确结果而非兜底。
        :raises sqlite3.Error: 查询失败。
        副作用：只读 ``pipeline_events``。
        """
        with self._lock:
            if self._connection is None:
                return 0
            row = self._connection.execute(
                "SELECT MAX(turn_id) FROM pipeline_events"
            ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    def first_matching_after(
        self,
        seq: int,
        *,
        kind: str,
        stream_id: int | None,
        turn_id: int | None,
    ) -> Dict[str, Any] | None:
        """读取指定事件之后同会话轮次的第一条目标类型事件。"""
        with self._lock:
            connection = self._require_connection()
            row = connection.execute(
                """
                SELECT seq, at, stream_id, turn_id, stage, kind, payload
                FROM pipeline_events
                WHERE seq > ? AND kind = ?
                  AND stream_id IS ? AND turn_id IS ?
                ORDER BY seq ASC
                LIMIT 1
                """,
                (seq, kind, stream_id, turn_id),
            ).fetchone()
        return self._row_to_event(row) if row is not None else None

    def close(self) -> None:
        """关闭事件账本连接并重置写入计数。

        副作用：关闭 SQLite 连接；重复调用安全。
        """
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._writes = 0

    def clear(self) -> None:
        """删除当前账本中的全部事件。

        副作用：清空 pipeline_events 表并提交事务；未配置连接时无操作。
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
        副作用：删除超出时长或数量上限的事件并提交事务。
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
        副作用：不执行 I/O。
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
        副作用：不修改 payload。
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
        副作用：不修改数据库行。
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
    副作用：委托全局 `event_store` 替换连接并创建表。
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


def max_turn_id() -> int:
    """读取模块级事件账本中已出现过的最大回合 ID。

    :return: 最大 ``turn_id``；账本为空或尚未配置时返回 ``0``。
    :raises sqlite3.Error: 查询失败。
    """
    return event_store.max_turn_id()


def search_events(
    *,
    stream_id: int | None = None,
    turn_id: int | None = None,
    kinds: List[str] | None = None,
    since_at: int | None = None,
    until_at: int | None = None,
    limit: int = 200,
    cursor: int | None = None,
) -> EventSearchPage:
    """检索模块级事件账本中的历史事件。

    参数语义与 :meth:`EventStore.search` 一致。
    """
    return event_store.search(
        stream_id=stream_id,
        turn_id=turn_id,
        kinds=kinds,
        since_at=since_at,
        until_at=until_at,
        limit=limit,
        cursor=cursor,
    )


def close() -> None:
    """关闭模块级事件账本连接。

    副作用：委托全局 `event_store` 关闭 SQLite 连接。
    """
    event_store.close()
