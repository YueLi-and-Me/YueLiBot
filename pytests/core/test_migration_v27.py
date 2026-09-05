"""群聊展示名列迁移（v26 -> v27）回归。"""

from __future__ import annotations

import sqlite3

from src.core.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    run_migrations,
)
from src.core.db.migrations.v26_to_v27 import FROM_VERSION, migrate
from src.core.db.schema import DDL, SEED


def _stream_columns(db: sqlite3.Connection) -> list[str]:
    """返回 streams 当前列名，保留 SQLite 声明顺序。"""

    return [str(row[1]) for row in db.execute("SELECT * FROM pragma_table_info('streams')")]


def test_v27_adds_nullable_stream_display_name_idempotently() -> None:
    """存量 stream 原样保留，展示名列可空，重复迁移不会重复加列。"""

    db = sqlite3.connect(':memory:')
    db.executescript(
        """
        CREATE TABLE streams (
          id INTEGER PRIMARY KEY,
          platform TEXT NOT NULL,
          kind TEXT NOT NULL,
          external_id TEXT NOT NULL,
          UNIQUE(platform, kind, external_id)
        );
        INSERT INTO streams(id, platform, kind, external_id)
        VALUES (7, 'qq', 'group', '629201002');
        """
    )

    assert FROM_VERSION == 26
    migrate(db)

    assert _stream_columns(db).count('display_name') == 1
    assert db.execute(
        'SELECT id, external_id, display_name FROM streams WHERE id = 7'
    ).fetchone() == (7, '629201002', None)

    migrate(db)
    assert _stream_columns(db).count('display_name') == 1
    db.close()


def test_migration_manager_advances_v26_database_to_v27() -> None:
    """迁移管理器必须真实登记本号并把原生版本推进到 27。"""

    db = sqlite3.connect(':memory:')
    db.executescript(DDL)
    db.executescript(SEED)
    db.execute('ALTER TABLE streams DROP COLUMN display_name')
    db.execute('PRAGMA user_version = 26')

    run_migrations(db)

    # 链头随版本推进：本迁移登记后库会继续走到当前声明的链头，不写死 27。
    assert get_user_version(db) == CURRENT_VERSION
    assert 'display_name' in _stream_columns(db)
    db.close()


def test_v27_leaves_early_database_without_streams_for_current_ddl() -> None:
    """极早期最小库没有 streams 时不猜表结构，由链尾 DDL 统一建表。"""

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE legacy_probe (id INTEGER PRIMARY KEY)')

    migrate(db)

    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'streams'"
    ).fetchone()[0] == 0
    db.close()


def test_current_ddl_creates_stream_display_name_column() -> None:
    """全新数据库无需历史迁移即可得到同一列定义。"""

    db = sqlite3.connect(':memory:')
    db.executescript(DDL)

    assert 'display_name' in _stream_columns(db)
    db.close()
