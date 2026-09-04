"""v26 -> v27：给会话流补充可空的可读展示名。

群名称属于 stream 自身的可变元数据，不另建关联表。全新数据库直接从当前 DDL
获得该列；本迁移只覆盖已经存在 ``streams`` 表的存量库，并允许安全重放。
"""

from __future__ import annotations

import sqlite3

from .registry import register

from src.core.common.logger import get_logger

logger = get_logger(__name__)

FROM_VERSION = 26


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    """判断目标表是否已经存在。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _stream_columns(db: sqlite3.Connection) -> set[str]:
    """读取 streams 当前列名。"""

    rows = db.execute("SELECT * FROM pragma_table_info('streams')").fetchall()
    return {str(row[1]) for row in rows}


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """给存量 streams 增加可空展示名列，缺表或已有列时保持幂等。"""

    if not _table_exists(db, 'streams'):
        return
    if 'display_name' in _stream_columns(db):
        return

    db.execute('ALTER TABLE streams ADD COLUMN display_name TEXT')
    if 'display_name' not in _stream_columns(db):
        raise RuntimeError('v26 -> v27 迁移后 streams.display_name 仍不存在')
    logger.info('v26_to_v27_done', added=['streams.display_name'])
