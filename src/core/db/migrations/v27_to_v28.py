"""v27 -> v28：事实操作流水表，供记忆的人工管理与自动取代留痕、审计与撤销。

新表 ``fact_operations`` 同时写进了当前 DDL，全新库由建表直接获得；本迁移只
覆盖存量库——只新建表与索引，不动 ``facts`` 的任何行。重放幂等：表已存在时
整体跳过，自检只在本次真正建表后断言。
"""

from __future__ import annotations

import sqlite3

from .registry import register

FROM_VERSION = 27

_EXPECTED_INDEXES = ('idx_fact_operations_fact', 'idx_fact_operations_person')


def _existing_tables(db: sqlite3.Connection) -> set[str]:
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _existing_indexes(db: sqlite3.Connection) -> set[str]:
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    return {str(row[0]) for row in rows}


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """为存量库建事实操作流水表与两个索引，重放时跳过已有结构。"""

    if 'fact_operations' in _existing_tables(db):
        return

    db.execute(
        '''CREATE TABLE IF NOT EXISTS fact_operations (
             id              INTEGER PRIMARY KEY AUTOINCREMENT,
             at              INTEGER NOT NULL,
             actor           TEXT    NOT NULL,
             op              TEXT    NOT NULL,
             person_id       INTEGER NOT NULL,
             fact_id         INTEGER NOT NULL,
             related_fact_id INTEGER,
             prev            TEXT    NOT NULL DEFAULT '{}',
             undone_by       INTEGER REFERENCES fact_operations(id),
             undo_of         INTEGER REFERENCES fact_operations(id)
           )'''
    )
    db.execute(
        'CREATE INDEX IF NOT EXISTS idx_fact_operations_fact '
        'ON fact_operations(fact_id)'
    )
    db.execute(
        'CREATE INDEX IF NOT EXISTS idx_fact_operations_person '
        'ON fact_operations(person_id, at)'
    )

    if 'fact_operations' not in _existing_tables(db):
        raise RuntimeError('v27 -> v28 迁移自检失败：fact_operations 未建成')
    missing = [name for name in _EXPECTED_INDEXES if name not in _existing_indexes(db)]
    if missing:
        raise RuntimeError(f'v27 -> v28 迁移自检失败：索引未建成 {missing}')
