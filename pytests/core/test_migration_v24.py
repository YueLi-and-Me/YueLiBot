"""N4 反馈纠错存储表迁移（v23 -> v24）回归。"""

from __future__ import annotations

import sqlite3

from src.core.db.migrations.bootstrap import write_user_version
from src.core.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    run_migrations,
)
from src.core.db.migrations.v23_to_v24 import FROM_VERSION, migrate


def _old_shape(db: sqlite3.Connection) -> None:
    """创建 v23 形态的最小库：facts 带账本列，episodes 没有待重建列。"""

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
          embedding BLOB, embedding_q8 BLOB, origin_kind TEXT NOT NULL DEFAULT 'legacy',
          slot TEXT NOT NULL DEFAULT '', superseded_by INTEGER REFERENCES facts(id),
          UNIQUE(person_id, content_key)
        );
        CREATE TABLE episodes (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'conversation',
          summary TEXT NOT NULL, started_at INTEGER NOT NULL, ended_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL, stream_id INTEGER NOT NULL DEFAULT 1
        );
        """
    )


def _tables(db: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_v24_creates_feedback_tables_and_episode_column() -> None:
    """两张反馈表与 episodes.needs_rebuild 就位；重放幂等。"""

    db = sqlite3.connect(':memory:')
    _old_shape(db)

    migrate(db)

    assert {'memory_feedback_pending', 'memory_feedback_results'} <= _tables(db)
    episode_cols = {str(r[1]) for r in db.execute("SELECT * FROM pragma_table_info('episodes')")}
    assert 'needs_rebuild' in episode_cols

    migrate(db)
    assert {'memory_feedback_pending', 'memory_feedback_results'} <= _tables(db)
    assert [r[1] for r in db.execute(
        "SELECT * FROM pragma_table_info('episodes')"
    )].count('needs_rebuild') == 1
    db.close()


def test_v24_accepts_early_database_without_episodes() -> None:
    """早期最小库没有 episodes 时不补列，留给链尾当前 DDL 建表。"""

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE legacy_probe (id INTEGER PRIMARY KEY)')

    migrate(db)

    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'episodes'"
    ).fetchone()[0] == 0
    # 两张反馈表不依赖 episodes，仍应建成。
    assert {'memory_feedback_pending', 'memory_feedback_results'} <= _tables(db)
    db.close()


def test_migration_chain_reaches_declared_head() -> None:
    """迁移管理器从本迁移的入口版本跑到当前链头，不在测试写死版本号。"""

    db = sqlite3.connect(':memory:')
    _old_shape(db)
    write_user_version(db, FROM_VERSION)

    run_migrations(db)

    assert get_user_version(db) == CURRENT_VERSION
    db.close()
