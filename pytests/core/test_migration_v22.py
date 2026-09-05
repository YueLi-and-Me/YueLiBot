"""W9 事实账本迁移回归。"""

from __future__ import annotations

import sqlite3

from src.core.db.migrations.bootstrap import write_user_version
from src.core.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    run_migrations,
)
from src.core.db.migrations.v21_to_v22 import FROM_VERSION, migrate


def _old_shape(db: sqlite3.Connection) -> None:
    """创建 v20 形态的最小库：facts 含 SQ8 列、没有账本两列。"""

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
        """
    )


def _columns(db: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in db.execute("SELECT * FROM pragma_table_info('facts')")}


def test_v22_adds_ledger_columns_and_conflict_index() -> None:
    """存量行保持空槽位与未取代；补列后写入默认值的形状与 DDL 一致。"""

    db = sqlite3.connect(':memory:')
    _old_shape(db)
    db.execute(
        '''INSERT INTO facts (id, person_id, kind, content, content_key, strength,
                              half_life_hours, updated_at, created_at, due_at)
           VALUES (1, 1, '身份', '他现在住在成都', 'k', 0.8, 2160, 1, 1, 2)'''
    )

    migrate(db)

    assert {'slot', 'superseded_by'} <= _columns(db)
    assert db.execute('SELECT slot, superseded_by FROM facts WHERE id = 1').fetchone() == ('', None)
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_facts_person_slot'"
    ).fetchone() is not None

    # 重放幂等：已有结构跳过，不重复加列。
    migrate(db)
    assert [row[1] for row in db.execute(
        "SELECT * FROM pragma_table_info('facts')"
    )].count('slot') == 1
    db.close()


def test_v22_accepts_early_database_without_facts_table() -> None:
    """早期最小库尚无 facts 时不猜结构，交给链尾当前 DDL 创建。"""

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE legacy_probe (id INTEGER PRIMARY KEY)')

    migrate(db)

    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'facts'"
    ).fetchone()[0] == 0
    db.close()


def test_migration_chain_reaches_declared_head() -> None:
    """迁移管理器从本迁移的入口版本跑到当前链头，不在测试写死版本号。"""

    db = sqlite3.connect(':memory:')
    _old_shape(db)
    write_user_version(db, FROM_VERSION)

    run_migrations(db)

    assert get_user_version(db) == CURRENT_VERSION
    assert {'slot', 'superseded_by'} <= _columns(db)
    db.close()
