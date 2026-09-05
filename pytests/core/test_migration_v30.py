"""运行画像表删除迁移（v29 -> v30）回归。

钉三件事：v29 建过表的存量库能把它删掉、重放幂等、全新库压根不会出现这张表。
第三条尤其重要——「建了又删」两步不能合并，真机已经越过 v29，
合并会让它跳过删表，留下一张没有消费方的表。
"""

from __future__ import annotations

import sqlite3

from src.core.db.migrations.bootstrap import write_user_version
from src.core.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    run_migrations,
)
from src.core.db.migrations.v28_to_v29 import migrate as migrate_v29
from src.core.db.migrations.v29_to_v30 import FROM_VERSION, migrate
from src.core.db.schema import DDL, SEED


def _existing(db: sqlite3.Connection, kind: str, name: str) -> bool:
    """判断 sqlite_master 里是否已有指定类型的对象。"""

    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?", (kind, name),
    ).fetchone() is not None


def _v29_shaped_db() -> sqlite3.Connection:
    """构造一个 v29 形态的库：当前 DDL 建表后，用 v29 迁移补出运行画像表。

    当前 DDL 已经不含这张表，所以必须走 v28 -> v29 才能得到 v29 的真实形态——
    这正是存量真机（09-05 已迁到 v29）现在的样子。
    """

    db = sqlite3.connect(':memory:')
    db.executescript(DDL)
    db.executescript(SEED)
    migrate_v29(db)
    write_user_version(db, 29)
    return db


def test_v30_drops_runtime_profile_from_a_v29_database() -> None:
    """v29 形态的库删掉画像表；其余表一行不动。"""

    db = _v29_shaped_db()
    db.execute("UPDATE runtime_profile SET install_id = 'x' WHERE id = 1")
    persons_before = db.execute('SELECT * FROM persons ORDER BY id').fetchall()

    assert FROM_VERSION == 29
    assert _existing(db, 'table', 'runtime_profile')

    migrate(db)

    assert not _existing(db, 'table', 'runtime_profile')
    assert db.execute('SELECT * FROM persons ORDER BY id').fetchall() == persons_before
    db.close()


def test_v30_replay_is_idempotent() -> None:
    """表已经不在时整体跳过，不抛异常。"""

    db = _v29_shaped_db()
    migrate(db)
    migrate(db)

    assert not _existing(db, 'table', 'runtime_profile')
    db.close()


def test_current_ddl_never_creates_runtime_profile() -> None:
    """全新库直接是 v30 形态：这张表从来不出现，不需要建了再删。"""

    db = sqlite3.connect(':memory:')
    db.executescript(DDL)

    assert not _existing(db, 'table', 'runtime_profile')
    db.close()


def test_chain_from_v28_builds_then_drops_the_table() -> None:
    """停在 v28 的存量库走完整条链后，表被建出来又被删掉，最终不存在。

    两步必须都在链条里：合并成「v28 直接到 v30」会让已经在 v29 的真机跳过删表。
    """

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE legacy_probe (id INTEGER PRIMARY KEY)')
    write_user_version(db, 28)

    run_migrations(db)

    assert get_user_version(db) == CURRENT_VERSION
    assert not _existing(db, 'table', 'runtime_profile')
    db.close()
