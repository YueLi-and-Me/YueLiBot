"""v14 -> v15：表情包库增加使用记录与按内容哈希的封禁表。

使用记录（use_count / last_used_at）是淘汰判据的新数据来源，与 seen_count
分开：seen_count 是「入站又见到这张图」（别人在用），而淘汰要看的是「她自己
发过几次」，两者混在一列会让淘汰保护「别人常发但她从不用」的图。

封禁表独立于 emoji 行存在，以内容哈希为主键：行被淘汰、文件被删之后封禁
必须仍然生效，同一张图不能因为删了一次就又进得来。
"""

from __future__ import annotations

import sqlite3

from .registry import register


_BANNED_DDL = """
CREATE TABLE emoji_banned (
  hash      TEXT PRIMARY KEY,
  banned_at INTEGER NOT NULL,
  reason    TEXT
);
"""


def _column_names(db: sqlite3.Connection) -> set[str]:
    """读取 emoji 表的当前列名集合，供幂等迁移核对。"""

    rows = db.execute("SELECT name FROM pragma_table_info('emoji')").fetchall()
    return {str(row[0]) for row in rows}


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    """判断目标表是否已经存在。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


@register(14)
def migrate(db: sqlite3.Connection) -> None:
    """给 emoji 表补使用记录两列，并创建按哈希的封禁表。

    旧数据没有使用记录，迁移后统一为 use_count = 0、last_used_at 为 NULL：
    那不是「从没用过」的断言，而是「v15 之前没有记录过使用」的事实，淘汰
    排序对这批行只按先入先出处理。
    """

    columns = _column_names(db)
    if 'use_count' not in columns:
        db.execute(
            'ALTER TABLE emoji ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0'
        )
    if 'last_used_at' not in columns:
        db.execute('ALTER TABLE emoji ADD COLUMN last_used_at INTEGER')

    if not _table_exists(db, 'emoji_banned'):
        db.executescript(_BANNED_DDL)

    banned_before = db.execute(
        'SELECT hash, banned_at, reason FROM emoji_banned ORDER BY hash'
    ).fetchall()
    if banned_before:
        raise RuntimeError('v15 迁移自检失败：新封禁表不是空表')
