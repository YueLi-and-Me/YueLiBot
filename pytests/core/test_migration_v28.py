"""事实操作流水表迁移（v27 -> v28）回归。"""

from __future__ import annotations

import sqlite3

from src.core.db.migrations.bootstrap import write_user_version
from src.core.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    run_migrations,
)
from src.core.db.migrations.v27_to_v28 import FROM_VERSION, migrate
from src.core.db.schema import DDL, SEED


def _existing(db: sqlite3.Connection, kind: str, name: str) -> bool:
    """判断 sqlite_master 里是否已有指定类型的对象。"""

    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?", (kind, name),
    ).fetchone() is not None


def _v27_shaped_db() -> sqlite3.Connection:
    """构造一个 v27 形态的库：当前 DDL 全量建表后摘掉 fact_operations。"""

    db = sqlite3.connect(':memory:')
    db.executescript(DDL)
    db.executescript(SEED)
    db.execute('DROP TABLE fact_operations')
    write_user_version(db, 27)
    return db


def test_v28_creates_fact_operations_table_and_indexes() -> None:
    """表与两个索引就位；迁移只新建结构，事实行零改动。"""

    db = _v27_shaped_db()
    db.execute(
        '''INSERT INTO facts (person_id, kind, content, content_key, strength,
                              half_life_hours, updated_at, created_at, due_at, active,
                              slot, origin_kind, superseded_by)
           VALUES (1, '身份', '他现在住在成都', 'ta xian zai zhu zai cheng du',
                   0.8, 504.0, 100, 100, 200, 1, '居住地', 'direct', NULL)'''
    )
    before = db.execute('SELECT * FROM facts ORDER BY id').fetchall()

    assert FROM_VERSION == 27
    migrate(db)

    assert _existing(db, 'table', 'fact_operations')
    assert _existing(db, 'index', 'idx_fact_operations_fact')
    assert _existing(db, 'index', 'idx_fact_operations_person')
    assert db.execute('SELECT * FROM facts ORDER BY id').fetchall() == before

    # 重放幂等：已有结构时整体跳过，不重复建表也不动数据。
    migrate(db)
    assert _existing(db, 'table', 'fact_operations')
    assert db.execute('SELECT * FROM facts ORDER BY id').fetchall() == before
    db.close()


def test_v28_accepts_early_database_without_facts() -> None:
    """极早期最小库也直接建流水表；它与其他表无依赖关系。"""

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE legacy_probe (id INTEGER PRIMARY KEY)')

    migrate(db)

    assert _existing(db, 'table', 'fact_operations')
    db.close()


def test_current_ddl_creates_fact_operations_table() -> None:
    """全新数据库无需历史迁移即可得到同一张表。"""

    db = sqlite3.connect(':memory:')
    db.executescript(DDL)

    assert _existing(db, 'table', 'fact_operations')
    assert _existing(db, 'index', 'idx_fact_operations_fact')
    assert _existing(db, 'index', 'idx_fact_operations_person')
    db.close()


def test_migration_chain_reaches_declared_head() -> None:
    """迁移管理器从本迁移的入口版本跑到当前链头，不在测试写死版本号。"""

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE legacy_probe (id INTEGER PRIMARY KEY)')
    write_user_version(db, FROM_VERSION)

    run_migrations(db)

    # 不写死链头版本号：它每次加迁移都会变，断言「跑到了链头」才是这条用例的意思。
    assert get_user_version(db) == CURRENT_VERSION
    assert _existing(db, 'table', 'fact_operations')
    db.close()
