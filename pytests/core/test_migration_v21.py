"""W7 origin_kind 列迁移回归。"""

from __future__ import annotations

import sqlite3

from src.core.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    run_migrations,
)
from src.core.db.migrations.v20_to_v21 import FROM_VERSION, migrate
from src.core.db.migrations.bootstrap import write_user_version


def _old_shape(db: sqlite3.Connection) -> None:
    """创建足以执行链尾 DDL 的 v20 目标表形态，不预置 origin_kind。"""

    db.executescript(
        """
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL,
          created_at INTEGER NOT NULL, episode_id INTEGER, stream_id INTEGER NOT NULL DEFAULT 1,
          sender_person_id INTEGER, external_message_id TEXT
        );
        CREATE TABLE facts (
          id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL DEFAULT 1,
          kind TEXT NOT NULL DEFAULT '事件', content TEXT NOT NULL,
          content_key TEXT NOT NULL, strength REAL NOT NULL,
          half_life_hours REAL NOT NULL, updated_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL, hit_count INTEGER NOT NULL DEFAULT 0,
          last_hit_at INTEGER, due_at INTEGER NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, tokens_v2 TEXT NOT NULL DEFAULT '',
          embedding BLOB, embedding_q8 BLOB, UNIQUE(person_id, content_key)
        );
        CREATE TABLE knowledge (
          id INTEGER PRIMARY KEY, content TEXT NOT NULL,
          content_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL DEFAULT '',
          tokens_v2 TEXT NOT NULL DEFAULT '', embedding BLOB, embedding_q8 BLOB,
          created_at INTEGER NOT NULL, hit_count INTEGER NOT NULL DEFAULT 0,
          last_hit_at INTEGER
        );
        """
    )


def test_v21_adds_origin_kind_and_backfills_legacy() -> None:
    """补列后存量行一律 legacy，不猜来源；重放保持幂等。"""

    db = sqlite3.connect(':memory:')
    _old_shape(db)
    db.execute(
        '''INSERT INTO facts (
             id, person_id, kind, content, content_key, strength, half_life_hours,
             updated_at, created_at, due_at
           ) VALUES (1, 1, '事件', '存量事实', 'fact', 0.8, 504, 1, 1, 2)''',
    )

    migrate(db)

    columns = [row[1] for row in db.execute("SELECT * FROM pragma_table_info('facts')")]
    assert 'origin_kind' in columns
    assert columns.count('origin_kind') == 1
    assert db.execute('SELECT origin_kind FROM facts').fetchone()[0] == 'legacy'

    migrate(db)
    assert [row[1] for row in db.execute(
        "SELECT * FROM pragma_table_info('facts')"
    )].count('origin_kind') == 1
    db.close()


def test_v21_accepts_early_database_without_facts_table() -> None:
    """早期最小库尚无 facts 表时不猜结构，交给链尾当前 DDL 创建。"""

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE legacy_probe (id INTEGER PRIMARY KEY)')

    migrate(db)

    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'facts'"
    ).fetchone()[0] == 0
    db.close()


def test_migration_chain_reaches_declared_head() -> None:
    """迁移管理器从模块声明的入口跑到当前链头，不在测试写死版本号。"""

    db = sqlite3.connect(':memory:')
    _old_shape(db)
    write_user_version(db, FROM_VERSION)

    run_migrations(db)

    assert get_user_version(db) == CURRENT_VERSION
    assert 'origin_kind' in {
        row[1] for row in db.execute("SELECT * FROM pragma_table_info('facts')")
    }
    db.close()
