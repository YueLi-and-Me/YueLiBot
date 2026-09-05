"""把历史聊天概括迁入 episodes 与 episode_cues。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import List, Optional, Set

import json
import sqlite3
import sys

# 入口既支持 ``python -m``，也支持直接执行文件；直接执行时先建立明确的包上下文，
# 后续仍使用同目录相对导入，不维护两套导入分支。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "scripts.migrate"

from .common import (
    SKIP_EMPTY_FIELD,
    SKIP_EXISTING,
    SKIP_UNMAPPED_SESSION,
    MigrationError,
    MigrationReport,
    load_stream_mapping,
    open_databases,
    run_cli,
    text_value,
    unix_milliseconds,
    unmapped_key,
    validate_query,
)
from src.core.memory.store import EpisodeInput, MemoryStore


_REASONS = (SKIP_UNMAPPED_SESSION, SKIP_EMPTY_FIELD, SKIP_EXISTING)


class _ExistingMemoryStore(MemoryStore):
    """只绑定已通过严格预检的现有库，不触发 MemoryStore 的建表与种子逻辑。"""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db


def migrate(source_path: Path, target_path: Path, dry_run: bool) -> MigrationReport:
    """迁移可映射且概括非空的历史情节。

    每条正式写入都走 ``MemoryStore.add_episode``，因此 episode、cue 与 contentless FTS
    的 rowid 在同一事务内保持一致。该方法逐条提交；中断后已完成部分由内容键识别，
    未完成部分会在重跑时继续。
    """

    report = MigrationReport("情节迁移", dry_run, _REASONS)
    with open_databases(source_path, target_path, dry_run) as (source_db, target_db):
        _validate_schema(source_db, target_db)
        stream_mapping = load_stream_mapping(source_db, target_db)
        existing_keys = _load_existing_keys(target_db)
        store: Optional[_ExistingMemoryStore] = None
        if not dry_run:
            store = _ExistingMemoryStore(target_db)

        rows = source_db.execute(
            """SELECT id, chat_id, start_time, end_time, theme, keywords, summary
               FROM chat_history ORDER BY id"""
        ).fetchall()
        report.source_rows = len(rows)
        for row in rows:
            row_id = int(row["id"])
            summary = text_value(row["summary"], "chat_history.summary", row_id)
            if not summary:
                report.record_skip(SKIP_EMPTY_FIELD)
                continue

            chat_id = text_value(row["chat_id"], "chat_history.chat_id", row_id)
            stream_id = stream_mapping.get(chat_id)
            if stream_id is None:
                report.record_skip(SKIP_UNMAPPED_SESSION, unmapped_key(chat_id))
                continue

            started_at = unix_milliseconds(row["start_time"], "chat_history.start_time", row_id)
            ended_at = unix_milliseconds(row["end_time"], "chat_history.end_time", row_id)
            if ended_at < started_at:
                raise MigrationError(f"chat_history 第 {row_id} 行 end_time 早于 start_time")
            theme = text_value(row["theme"], "chat_history.theme", row_id)
            merged_summary = _merge_theme(theme, summary)
            cues = _parse_keywords(row["keywords"], row_id)
            content_key = _episode_key(stream_id, started_at, merged_summary)
            if content_key in existing_keys:
                report.record_skip(SKIP_EXISTING)
                continue

            if dry_run:
                report.record_write()
                existing_keys.add(content_key)
                continue

            if store is None:
                raise MigrationError("正式迁移未绑定目标 MemoryStore")
            try:
                store.add_episode(
                    stream_id,
                    EpisodeInput(
                        summary=merged_summary,
                        cues=cues,
                        started_at=started_at,
                        ended_at=ended_at,
                        message_ids=[],
                        kind="conversation",
                    ),
                    now=ended_at,
                )
            except sqlite3.Error as exc:
                target_db.rollback()
                raise MigrationError(f"chat_history 第 {row_id} 行写入失败：{exc}") from exc
            report.record_write()
            existing_keys.add(content_key)
    return report


def _validate_schema(source_db: sqlite3.Connection, target_db: sqlite3.Connection) -> None:
    """确认 M-1 所需字段和 FTS 表都已由地基提供。"""

    validate_query(
        source_db,
        """SELECT id, chat_id, start_time, end_time, theme, keywords, summary
           FROM chat_history LIMIT 0""",
        "源库 chat_history ",
    )
    validate_query(
        target_db,
        """SELECT id, stream_id, kind, summary, started_at, ended_at, created_at
           FROM episodes LIMIT 0""",
        "目标库 episodes ",
    )
    validate_query(
        target_db,
        "SELECT id, episode_id, cue FROM episode_cues LIMIT 0",
        "目标库 episode_cues ",
    )
    validate_query(target_db, "SELECT rowid, tokens FROM cues_fts LIMIT 0", "目标库 cues_fts ")


def _load_existing_keys(target_db: sqlite3.Connection) -> Set[str]:
    """读取所有既有情节的稳定内容键，供 dry-run 与正式执行共用。"""

    return {
        _episode_key(int(row["stream_id"]), int(row["started_at"]), str(row["summary"]))
        for row in target_db.execute("SELECT stream_id, started_at, summary FROM episodes")
    }


def _episode_key(stream_id: int, started_at: int, summary: str) -> str:
    """生成 ``(stream_id, started_at, summary)`` 的无歧义 SHA-256 内容键。"""

    payload = json.dumps(
        [stream_id, started_at, summary],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _merge_theme(theme: str, summary: str) -> str:
    """把非空主题作为历史概括首行，保留其标题语义。"""

    return f"主题：{theme}\n{summary}" if theme else summary


def _parse_keywords(raw: object, row_id: int) -> List[str]:
    """按已核实的 JSON 字符串数组格式解析历史关键词并保持原顺序去重。"""

    text = text_value(raw, "chat_history.keywords", row_id)
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"chat_history 第 {row_id} 行 keywords 不是合法 JSON") from exc
    if not isinstance(decoded, list):
        raise MigrationError(f"chat_history 第 {row_id} 行 keywords 必须是 JSON 数组")

    cues: List[str] = []
    seen: Set[str] = set()
    for item in decoded:
        if not isinstance(item, str):
            raise MigrationError(f"chat_history 第 {row_id} 行 keywords 含非文本项")
        cue = item.strip()
        if cue and cue not in seen:
            cues.append(cue)
            seen.add(cue)
    return cues


def main() -> int:
    """运行 M-1 命令行入口。"""

    return run_cli("迁移历史情节到 episodes 与 episode_cues", migrate)


if __name__ == "__main__":
    raise SystemExit(main())
