"""W2-M5：把历史人物 impression 作为待刷新的画像种子迁入。"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import sqlite3
import sys

# 直接执行文件时建立包上下文，让入口与 ``python -m`` 共用同一套相对导入。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "scripts.migrate"

from .common import (
    SKIP_EMPTY_FIELD,
    SKIP_EXISTING,
    SKIP_UNMAPPED_IDENTITY,
    MigrationReport,
    load_qq_identity_mapping,
    open_databases,
    run_cli,
    text_value,
    unmapped_key,
    validate_query,
)


_COMMIT_ROWS = 50
_REASONS = (SKIP_UNMAPPED_IDENTITY, SKIP_EMPTY_FIELD, SKIP_EXISTING)


def migrate(source_path: Path, target_path: Path, dry_run: bool) -> MigrationReport:
    """迁移非空画像种子，并保护目标库已有的非空画像。

    新行和空画像行都会写成 ``evidence_count=0, refreshed_at=0, dirty=1``。已有非空画像
    可能已经由本地证据刷新，因此无论内容是否相同都只计为已存在，不做覆盖或合并。
    """

    report = MigrationReport("W2-M5 画像种子迁移", dry_run, _REASONS)
    with open_databases(source_path, target_path, dry_run) as (source_db, target_db):
        _validate_schema(source_db, target_db)
        identity_mapping = load_qq_identity_mapping(target_db)
        profiles = _load_profiles(target_db)
        pending_writes = 0

        rows = source_db.execute(
            "SELECT id, platform, user_id, impression FROM person_info ORDER BY id"
        ).fetchall()
        report.source_rows = len(rows)
        for row in rows:
            row_id = int(row["id"])
            summary = text_value(row["impression"], "person_info.impression", row_id)
            if not summary:
                report.record_skip(SKIP_EMPTY_FIELD)
                continue

            platform = text_value(row["platform"], "person_info.platform", row_id).lower()
            external_id = text_value(row["user_id"], "person_info.user_id", row_id)
            if not external_id:
                report.record_skip(SKIP_EMPTY_FIELD)
                continue
            person_id = identity_mapping.get(external_id) if platform == "qq" else None
            if person_id is None:
                report.record_skip(SKIP_UNMAPPED_IDENTITY, unmapped_key(external_id))
                continue

            existing_summary = profiles.get(person_id)
            if existing_summary:
                report.record_skip(SKIP_EXISTING)
                continue

            if dry_run:
                report.record_write()
                profiles[person_id] = summary
                continue

            if person_id in profiles:
                cursor = target_db.execute(
                    """UPDATE person_profile
                       SET summary = ?, evidence_count = 0, refreshed_at = 0, dirty = 1
                       WHERE person_id = ? AND trim(summary) = ''""",
                    (summary, person_id),
                )
            else:
                cursor = target_db.execute(
                    """INSERT OR IGNORE INTO person_profile
                       (person_id, summary, evidence_count, refreshed_at, dirty)
                       VALUES (?, ?, 0, 0, 1)""",
                    (person_id, summary),
                )
            if cursor.rowcount == 0:
                report.record_skip(SKIP_EXISTING)
            else:
                report.record_write()
                pending_writes += 1
            profiles[person_id] = summary
            if pending_writes >= _COMMIT_ROWS:
                target_db.commit()
                pending_writes = 0
        if not dry_run:
            target_db.commit()
    return report


def _load_profiles(target_db: sqlite3.Connection) -> Dict[int, str]:
    """读取人物画像现状；空字符串保留为可填充的已有行。"""

    return {
        int(row["person_id"]): str(row["summary"]).strip()
        for row in target_db.execute("SELECT person_id, summary FROM person_profile")
    }


def _validate_schema(source_db: sqlite3.Connection, target_db: sqlite3.Connection) -> None:
    """确认 M-5 的历史画像字段和地基画像表均已存在。"""

    validate_query(
        source_db,
        "SELECT id, platform, user_id, impression FROM person_info LIMIT 0",
        "源库 person_info ",
    )
    validate_query(
        target_db,
        """SELECT person_id, summary, evidence_count, refreshed_at, dirty
           FROM person_profile LIMIT 0""",
        "目标库 person_profile ",
    )


def main() -> int:
    """运行 M-5 命令行入口。"""

    return run_cli("迁移历史 impression 作为待刷新画像种子", migrate)


if __name__ == "__main__":
    raise SystemExit(main())
