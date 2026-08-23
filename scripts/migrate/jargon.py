"""W2-M3：把已确认且有释义的历史黑话迁入 jargon。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set, Tuple

import sqlite3
import sys

# 直接执行文件时建立包上下文，让入口与 ``python -m`` 共用同一套相对导入。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "scripts.migrate"

from .common import (
    SKIP_EMPTY_FIELD,
    SKIP_EXISTING,
    SKIP_PENDING,
    SKIP_REJECTED,
    SKIP_UNMAPPED_SESSION,
    MigrationError,
    MigrationReport,
    load_stream_mapping,
    now_milliseconds,
    open_databases,
    run_cli,
    sqlite_boolean,
    text_value,
    unmapped_key,
    validate_query,
)


_COMMIT_ROWS = 200
_REASONS = (
    SKIP_UNMAPPED_SESSION,
    SKIP_EMPTY_FIELD,
    SKIP_EXISTING,
    SKIP_PENDING,
    SKIP_REJECTED,
)
_SOURCE_MARKER = "历史迁移:M-3"


def migrate(source_path: Path, target_path: Path, dry_run: bool) -> MigrationReport:
    """迁移 ``is_jargon=1``、释义非空且作用域可解析的黑话。

    全局词显式写 ``stream_id=NULL``，局部词必须映射到既有会话。SQLite 唯一约束不会
    把两个 NULL 视为冲突，因此脚本也在内存中维护 ``(term, stream_id)`` 内容键，确保
    全局词重复执行同样幂等。
    """

    report = MigrationReport("W2-M3 黑话迁移", dry_run, _REASONS)
    with open_databases(source_path, target_path, dry_run) as (source_db, target_db):
        _validate_schema(source_db, target_db)
        stream_mapping = load_stream_mapping(source_db, target_db)
        existing_keys = _load_existing_keys(target_db)
        migration_at = now_milliseconds()
        pending_writes = 0

        rows = source_db.execute(
            """SELECT id, content, meaning, chat_id, is_global, is_jargon
               FROM jargon ORDER BY id"""
        ).fetchall()
        report.source_rows = len(rows)
        for row in rows:
            row_id = int(row["id"])
            is_jargon = sqlite_boolean(row["is_jargon"], "jargon.is_jargon", row_id)
            if is_jargon is None:
                report.record_skip(SKIP_PENDING)
                continue
            if is_jargon is False:
                report.record_skip(SKIP_REJECTED)
                continue

            term = text_value(row["content"], "jargon.content", row_id)
            meaning = text_value(row["meaning"], "jargon.meaning", row_id)
            if not term or not meaning:
                report.record_skip(SKIP_EMPTY_FIELD)
                continue

            is_global = sqlite_boolean(row["is_global"], "jargon.is_global", row_id)
            if is_global is None:
                raise MigrationError(f"jargon 第 {row_id} 行 is_global 不能为 NULL")
            stream_id: Optional[int] = None
            if not is_global:
                chat_id = text_value(row["chat_id"], "jargon.chat_id", row_id)
                stream_id = stream_mapping.get(chat_id)
                if stream_id is None:
                    report.record_skip(SKIP_UNMAPPED_SESSION, unmapped_key(chat_id))
                    continue

            content_key = (term, stream_id)
            if content_key in existing_keys:
                report.record_skip(SKIP_EXISTING)
                continue

            if dry_run:
                report.record_write()
                existing_keys.add(content_key)
                continue

            cursor = target_db.execute(
                """INSERT OR IGNORE INTO jargon
                   (term, meaning, stream_id, status, hits, source, created_at)
                   VALUES (?, ?, ?, 'confirmed', 0, ?, ?)""",
                (term, meaning, stream_id, _SOURCE_MARKER, migration_at),
            )
            if cursor.rowcount == 0:
                report.record_skip(SKIP_EXISTING)
            else:
                report.record_write()
                pending_writes += 1
            existing_keys.add(content_key)
            if pending_writes >= _COMMIT_ROWS:
                target_db.commit()
                pending_writes = 0
        if not dry_run:
            target_db.commit()
    return report


def _load_existing_keys(
    target_db: sqlite3.Connection,
) -> Set[Tuple[str, Optional[int]]]:
    """读取目标黑话表的全局与会话内内容键。"""

    keys: Set[Tuple[str, Optional[int]]] = set()
    for row in target_db.execute("SELECT term, stream_id FROM jargon"):
        raw_stream_id = row["stream_id"]
        stream_id = int(raw_stream_id) if raw_stream_id is not None else None
        keys.add((str(row["term"]), stream_id))
    return keys


def _validate_schema(source_db: sqlite3.Connection, target_db: sqlite3.Connection) -> None:
    """确认 M-3 的源字段和地基黑话表均已存在。"""

    validate_query(
        source_db,
        """SELECT id, content, meaning, chat_id, is_global, is_jargon
           FROM jargon LIMIT 0""",
        "源库 jargon ",
    )
    validate_query(
        target_db,
        """SELECT term, meaning, stream_id, status, hits, source, created_at
           FROM jargon LIMIT 0""",
        "目标库 jargon ",
    )


def main() -> int:
    """运行 M-3 命令行入口。"""

    return run_cli("迁移已确认历史黑话到 jargon", migrate)


if __name__ == "__main__":
    raise SystemExit(main())
