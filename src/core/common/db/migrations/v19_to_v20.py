"""v19 -> v20：为事实与知识向量增加并存的 SQ8 列。

量化列是从原始 ``embedding`` 派生的数据。迁移只补列，不量化、不删除也不改写
原列；存量补算由启动期任务或一次性脚本执行，使 DDL 事务保持短小可审计。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import sqlite3

from .registry import register

FROM_VERSION = 19


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    """判断历史库是否已经包含目标表。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _shape(db: sqlite3.Connection, table: str) -> Dict[str, str]:
    """读取固定目标表的列名与类型；调用方只传本模块内的两个字面量。"""

    if table == 'facts':
        rows = db.execute("SELECT * FROM pragma_table_info('facts')").fetchall()
    elif table == 'knowledge':
        rows = db.execute("SELECT * FROM pragma_table_info('knowledge')").fetchall()
    else:
        raise ValueError(f'不支持的 v20 迁移目标表：{table}')
    return {str(row[1]): str(row[2]).upper() for row in rows}


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """为已存在的目标表补 ``embedding_q8 BLOB``，重放时跳过已有列。

    部分很早的最小测试库尚无 ``facts`` 或 ``knowledge``，它们会由迁移链尾的
    当前 DDL 按 v20 完整结构创建。自检只覆盖本次真正执行的 ALTER TABLE。
    """

    added: List[Tuple[str, str]] = []
    if _table_exists(db, 'facts') and 'embedding_q8' not in _shape(db, 'facts'):
        # 列名和类型均为静态字面量，不以字符串拼接生成 DDL。
        db.execute('ALTER TABLE facts ADD COLUMN embedding_q8 BLOB')
        added.append(('facts', 'embedding_q8'))
    if _table_exists(db, 'knowledge') and 'embedding_q8' not in _shape(db, 'knowledge'):
        db.execute('ALTER TABLE knowledge ADD COLUMN embedding_q8 BLOB')
        added.append(('knowledge', 'embedding_q8'))

    for table, column in added:
        actual_type = _shape(db, table).get(column)
        if actual_type != 'BLOB':
            raise RuntimeError(
                f'v20 迁移自检失败：{table}.{column} 类型是 '
                f'{actual_type!r}，预期 BLOB'
            )
