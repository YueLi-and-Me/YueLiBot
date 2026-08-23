"""W2-M2：把已审核的历史表达方式迁入 expressions。"""

from __future__ import annotations

from pathlib import Path
from typing import Set, Tuple

import sqlite3
import sys

# 直接执行文件时建立包上下文，让入口与 ``python -m`` 共用同一套相对导入。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "scripts.migrate"

from .common import (
    SKIP_EMPTY_FIELD,
    SKIP_EXISTING,
    SKIP_UNCHECKED,
    SKIP_UNMAPPED_SESSION,
    MigrationReport,
    load_stream_mapping,
    non_negative_integer,
    now_milliseconds,
    open_databases,
    run_cli,
    sqlite_boolean,
    text_value,
    unix_milliseconds,
    unmapped_key,
    validate_query,
)


_COMMIT_ROWS = 200
_REASONS = (
    SKIP_UNMAPPED_SESSION,
    SKIP_EMPTY_FIELD,
    SKIP_EXISTING,
    SKIP_UNCHECKED,
)
_SOURCE_MARKER = "历史迁移:M-2"


def migrate(source_path: Path, target_path: Path, dry_run: bool) -> MigrationReport:
    """迁移有会话归属、字段完整且已经审核的表达方式。

    正式模式每 200 条提交一次；进程在批次之间中断时，已经提交的内容由
    ``(situation, style, stream_id)`` 内容键识别，重跑不会重复新增。
    """

    report = MigrationReport("W2-M2 表达迁移", dry_run, _REASONS)
    with open_databases(source_path, target_path, dry_run) as (source_db, target_db):
        _validate_schema(source_db, target_db)
        stream_mapping = load_stream_mapping(source_db, target_db)
        existing_keys = _load_existing_keys(target_db)
        migration_at = now_milliseconds()
        pending_writes = 0

        rows = source_db.execute(
            """SELECT id, situation, style, count, chat_id, checked, create_date
               FROM expression ORDER BY id"""
        ).fetchall()
        report.source_rows = len(rows)
        for row in rows:
            row_id = int(row["id"])
            checked = sqlite_boolean(row["checked"], "expression.checked", row_id)
            if checked is not True:
                report.record_skip(SKIP_UNCHECKED)
                continue

            situation = text_value(row["situation"], "expression.situation", row_id)
            style = text_value(row["style"], "expression.style", row_id)
            if not situation or not style:
                report.record_skip(SKIP_EMPTY_FIELD)
                continue

            chat_id = text_value(row["chat_id"], "expression.chat_id", row_id)
            stream_id = stream_mapping.get(chat_id)
            if stream_id is None:
                report.record_skip(SKIP_UNMAPPED_SESSION, unmapped_key(chat_id))
                continue

            use_count = non_negative_integer(row["count"], "expression.count", row_id)
            created_at = _created_at(row["create_date"], row_id, migration_at)
            content_key = (situation, style, stream_id)
            if content_key in existing_keys:
                report.record_skip(SKIP_EXISTING)
                continue

            if dry_run:
                report.record_write()
                existing_keys.add(content_key)
                continue

            cursor = target_db.execute(
                """INSERT OR IGNORE INTO expressions
                   (situation, style, stream_id, use_count, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (situation, style, stream_id, use_count, _SOURCE_MARKER, created_at),
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


def _created_at(value: object, row_id: int, migration_at: int) -> int:
    """保留有效的历史创建时间；旧结构允许 NULL，此时记录本次迁移时间。"""

    if value is None:
        return migration_at
    return unix_milliseconds(value, "expression.create_date", row_id)


def _load_existing_keys(target_db: sqlite3.Connection) -> Set[Tuple[str, str, int]]:
    """读取目标表达表的唯一内容键。"""

    keys: Set[Tuple[str, str, int]] = set()
    for row in target_db.execute(
        "SELECT situation, style, stream_id FROM expressions WHERE stream_id IS NOT NULL"
    ):
        keys.add((str(row["situation"]), str(row["style"]), int(row["stream_id"])))
    return keys


def _validate_schema(source_db: sqlite3.Connection, target_db: sqlite3.Connection) -> None:
    """确认 M-2 的源字段和地基表达表均已存在。"""

    validate_query(
        source_db,
        """SELECT id, situation, style, count, chat_id, checked, create_date
           FROM expression LIMIT 0""",
        "源库 expression ",
    )
    validate_query(
        target_db,
        """SELECT situation, style, stream_id, use_count, source, created_at
           FROM expressions LIMIT 0""",
        "目标库 expressions ",
    )


def main() -> int:
    """运行 M-2 命令行入口。"""

    return run_cli("迁移历史表达方式到 expressions", migrate)


if __name__ == "__main__":
    raise SystemExit(main())
