"""v24 -> v25：导入中心的来源批次表与知识批次列。

``import_batches`` 登记每次导入的来源与统计：谁导的、什么时候、原始文件名
或粘贴摘要、提交条目数、实际新增条目数、状态。``knowledge.import_batch_id``
可空外键指向批次——存量 22498 条保持 NULL，语义是「迁移进来的，无批次」；
按批次撤销只删指向该批次的行，NULL 行永不命中。

新表与列同时写进当前 DDL，全新库由建表直接获得；本迁移覆盖的是存量库。
重放幂等：已存在的表与列跳过，自检只在本次真正执行了 DDL 时才断言。
"""

from __future__ import annotations

from typing import List

import sqlite3

from .registry import register

FROM_VERSION = 24


def _existing_tables(db: sqlite3.Connection) -> set[str]:
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _knowledge_columns(db: sqlite3.Connection) -> set[str]:
    rows = db.execute("SELECT * FROM pragma_table_info('knowledge')").fetchall()
    return {str(row[1]) for row in rows}


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """建来源批次表并给 knowledge 补批次列，重放时跳过已有结构。"""

    existing = _existing_tables(db)
    added: List[str] = []
    if 'import_batches' not in existing:
        db.execute(
            '''CREATE TABLE import_batches (
                 id           INTEGER PRIMARY KEY,
                 kind         TEXT    NOT NULL,
                 origin_name  TEXT    NOT NULL DEFAULT '',
                 summary      TEXT    NOT NULL DEFAULT '',
                 submitted    INTEGER NOT NULL DEFAULT 0,
                 added        INTEGER NOT NULL DEFAULT 0,
                 status       TEXT    NOT NULL DEFAULT 'running',
                 created_at   INTEGER NOT NULL,
                 finished_at  INTEGER,
                 error        TEXT    NOT NULL DEFAULT ''
               )'''
        )
        db.execute(
            'CREATE INDEX idx_import_batches_created ON import_batches(created_at)'
        )
        added.append('import_batches')
    # knowledge 可能尚未建表（早期形态的库一路走上来）：ALTER 无表可改会炸，
    # 缺表时跳过，由链尾 DDL 的 CREATE TABLE IF NOT EXISTS 建出全形态。
    if 'knowledge' in existing and 'import_batch_id' not in _knowledge_columns(db):
        # NULL 表示无批次：历史迁移进来的存量与运行期抽取写入的知识都没有批次，
        # 按批次删除的 WHERE 子句不含 NULL 行，撤销操作永远碰不到它们。
        db.execute(
            'ALTER TABLE knowledge ADD COLUMN import_batch_id INTEGER'
            ' REFERENCES import_batches(id) ON DELETE CASCADE'
        )
        db.execute(
            'CREATE INDEX idx_knowledge_import_batch ON knowledge(import_batch_id)'
        )
        added.append('knowledge.import_batch_id')

    if not added:
        return
    if 'knowledge.import_batch_id' in added:
        shape = _knowledge_columns(db)
        assert 'import_batch_id' in shape, 'v25 迁移自检失败：knowledge 缺 import_batch_id'
    tables = _existing_tables(db)
    assert 'import_batches' in tables, 'v25 迁移自检失败：import_batches 未建成'
