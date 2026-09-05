"""人物画像信任分级迁移（v25 -> v26）回归。"""

from __future__ import annotations

import sqlite3

from src.core.db.migrations.bootstrap import write_user_version
from src.core.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    run_migrations,
)
from src.core.db.migrations.v25_to_v26 import FROM_VERSION, migrate


def _old_shape(db: sqlite3.Connection) -> None:
    """创建 v25 形态的最小库：person_profile 没有确凿档与证据指纹两列。"""

    db.executescript(
        """
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL,
          created_at INTEGER NOT NULL, episode_id INTEGER, stream_id INTEGER NOT NULL DEFAULT 1,
          sender_person_id INTEGER, external_message_id TEXT
        );
        CREATE TABLE persons (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, first_seen_at INTEGER NOT NULL
        );
        CREATE TABLE person_profile (
          person_id      INTEGER PRIMARY KEY REFERENCES persons(id) ON DELETE CASCADE,
          summary        TEXT    NOT NULL DEFAULT '',
          evidence_count INTEGER NOT NULL DEFAULT 0,
          refreshed_at   INTEGER NOT NULL DEFAULT 0,
          dirty          INTEGER NOT NULL DEFAULT 1
        );
        """
    )


def _columns(db: sqlite3.Connection) -> list[str]:
    return [str(r[1]) for r in db.execute("SELECT * FROM pragma_table_info('person_profile')")]


def test_v26_adds_confirmed_and_fingerprint_columns() -> None:
    """两列就位；存量行的 summary 原样保留为印象档，确凿档留空（不重算、不编出处）。"""

    db = sqlite3.connect(':memory:')
    _old_shape(db)
    db.execute("INSERT INTO persons (id, kind, first_seen_at) VALUES (7, 'contact', 1)")
    db.execute(
        "INSERT INTO person_profile (person_id, summary, evidence_count, refreshed_at, dirty) "
        "VALUES (7, '旧版画像整段保留', 5, 123, 0)"
    )

    migrate(db)

    assert 'confirmed' in _columns(db)
    assert 'evidence_fingerprint' in _columns(db)
    row = db.execute(
        'SELECT summary, confirmed, evidence_fingerprint, evidence_count FROM person_profile '
        'WHERE person_id = 7'
    ).fetchone()
    assert row == ('旧版画像整段保留', '', '', 5)

    # 重放幂等：已有列跳过，数据不动。
    migrate(db)
    assert _columns(db).count('confirmed') == 1
    assert db.execute(
        'SELECT summary FROM person_profile WHERE person_id = 7'
    ).fetchone()[0] == '旧版画像整段保留'
    db.close()


def test_v26_accepts_early_database_without_person_profile() -> None:
    """早期最小库没有 person_profile 时不加列，留给链尾当前 DDL 建表。"""

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE legacy_probe (id INTEGER PRIMARY KEY)')

    migrate(db)

    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'person_profile'"
    ).fetchone()[0] == 0
    db.close()


def test_migration_chain_reaches_declared_head() -> None:
    """迁移管理器从本迁移的入口版本跑到当前链头，不在测试写死版本号。"""

    db = sqlite3.connect(':memory:')
    _old_shape(db)
    write_user_version(db, FROM_VERSION)

    run_migrations(db)

    assert get_user_version(db) == CURRENT_VERSION
    assert 'confirmed' in _columns(db)
    db.close()
