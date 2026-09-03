"""v21 -> v22：facts 增加事实账本两列与冲突检测索引。

``slot`` 标记「同一个人身上只可能有一个取值」的单值槽位（居住地、职业、生日……），
多值事实（喜好、经历）留空；同一 ``(person_id, slot)`` 下出现异值时两条都保留，
注入提示词时并排呈现，不按时间或分数静默覆盖。
``superseded_by`` 记录显式取代：非空即已失效，同时构成取代链指向新行。

迁移只补列与索引，存量行保持 ``slot = ''``、``superseded_by = NULL``，
由后续抽取逐步填槽。
"""

from __future__ import annotations

from typing import Dict, List

import sqlite3

from .registry import register

FROM_VERSION = 21

# 列名和类型均为静态字面量，不以字符串拼接生成 DDL。
_EXPECTED_TYPES = {'slot': 'TEXT', 'superseded_by': 'INTEGER'}


def _facts_exists(db: sqlite3.Connection) -> bool:
    """判断历史库是否已经包含 ``facts``；部分早期最小库要到链尾 DDL 才建表。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'facts'"
    ).fetchone()
    return row is not None


def _shape(db: sqlite3.Connection) -> Dict[str, str]:
    """读取 facts 的列名与类型。"""

    rows = db.execute("SELECT * FROM pragma_table_info('facts')").fetchall()
    return {str(row[1]): str(row[2]).upper() for row in rows}


def _index_exists(db: sqlite3.Connection) -> bool:
    """判断冲突检测索引是否已经存在。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_facts_person_slot'"
    ).fetchone()
    return row is not None


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """为已存在的 facts 补两列与索引，重放时跳过已有结构。

    自检只覆盖本次真正执行的 DDL：补过的列逐列核对类型，建过的索引核对存在性。
    """

    if not _facts_exists(db):
        return
    shape = _shape(db)
    added: List[str] = []
    if 'slot' not in shape:
        db.execute("ALTER TABLE facts ADD COLUMN slot TEXT NOT NULL DEFAULT ''")
        added.append('slot')
    if 'superseded_by' not in shape:
        db.execute('ALTER TABLE facts ADD COLUMN superseded_by INTEGER REFERENCES facts(id)')
        added.append('superseded_by')
    index_added = False
    if not _index_exists(db):
        db.execute('CREATE INDEX idx_facts_person_slot ON facts(person_id, slot)')
        index_added = True

    for column in added:
        actual = _shape(db).get(column)
        if actual != _EXPECTED_TYPES[column]:
            raise RuntimeError(
                f'v22 迁移自检失败：facts.{column} 类型是 {actual!r}，'
                f'预期 {_EXPECTED_TYPES[column]!r}'
            )
    if index_added and not _index_exists(db):
        raise RuntimeError('v22 迁移自检失败：idx_facts_person_slot 未建成')
