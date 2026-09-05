"""N2 SQ8 列迁移回归。"""

from __future__ import annotations

import sqlite3

from src.core.common.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    run_migrations,
)
from src.core.common.db.migrations.v19_to_v20 import FROM_VERSION, migrate
from src.core.common.db.migrations.bootstrap import write_user_version


def _old_shape(db: sqlite3.Connection) -> None:
    """创建足以执行链尾 DDL 的 v19 目标表形态，不预置 v20 两列。"""

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
          embedding BLOB, UNIQUE(person_id, content_key)
        );
        CREATE TABLE knowledge (
          id INTEGER PRIMARY KEY, content TEXT NOT NULL,
          content_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL DEFAULT '',
          tokens_v2 TEXT NOT NULL DEFAULT '', embedding BLOB,
          created_at INTEGER NOT NULL, hit_count INTEGER NOT NULL DEFAULT 0,
          last_hit_at INTEGER
        );
        """
    )


def test_v20_adds_only_q8_columns_and_preserves_raw_embedding() -> None:
    """★Q-2：迁移补两列，原始向量逐字节不变，重放保持幂等。"""

    db = sqlite3.connect(':memory:')
    _old_shape(db)
    fact_raw = bytes(range(16))
    knowledge_raw = bytes(reversed(range(16)))
    db.execute(
        '''INSERT INTO facts (
             id, person_id, kind, content, content_key, strength, half_life_hours,
             updated_at, created_at, due_at, embedding
           ) VALUES (1, 1, '事件', '事实', 'fact', 0.8, 504, 1, 1, 2, ?)''',
        (fact_raw,),
    )
    db.execute(
        '''INSERT INTO knowledge (
             id, content, content_key, source, created_at, embedding
           ) VALUES (1, '知识', 'knowledge', 'test', 1, ?)''',
        (knowledge_raw,),
    )

    migrate(db)

    assert 'embedding_q8' in {
        row[1] for row in db.execute("SELECT * FROM pragma_table_info('facts')")
    }
    assert 'embedding_q8' in {
        row[1] for row in db.execute("SELECT * FROM pragma_table_info('knowledge')")
    }
    assert db.execute('SELECT embedding, embedding_q8 FROM facts').fetchone() == (
        fact_raw,
        None,
    )
    assert db.execute('SELECT embedding, embedding_q8 FROM knowledge').fetchone() == (
        knowledge_raw,
        None,
    )

    migrate(db)
    assert [row[1] for row in db.execute(
        "SELECT * FROM pragma_table_info('facts')"
    )].count('embedding_q8') == 1
    db.close()


def test_v20_accepts_early_database_without_target_tables() -> None:
    """早期最小库尚无目标表时不猜结构，交给链尾当前 DDL 创建。"""

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE legacy_probe (id INTEGER PRIMARY KEY)')

    migrate(db)

    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('facts', 'knowledge')"
    ).fetchone()[0] == 0
    db.close()


def test_migration_chain_reaches_declared_head() -> None:
    """迁移管理器从模块声明的入口跑到当前链头，不在测试写死版本号。"""

    db = sqlite3.connect(':memory:')
    _old_shape(db)
    write_user_version(db, FROM_VERSION)

    run_migrations(db)

    assert get_user_version(db) == CURRENT_VERSION
    assert 'embedding_q8' in {
        row[1] for row in db.execute("SELECT * FROM pragma_table_info('facts')")
    }
    assert 'embedding_q8' in {
        row[1] for row in db.execute("SELECT * FROM pragma_table_info('knowledge')")
    }
    db.close()
