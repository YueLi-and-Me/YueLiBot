"""v20 -> v21：为事实增加被听见场合的来源标记列。

``origin_kind`` 只记录这条事实最初在哪种 stream 里被写下，可见性判定在读取侧
由 ``src/core.memory.scope`` 完成。迁移只补列：存量行一律保持默认值 ``legacy``，
不按任何推断回填——用一次猜测给已有数据编造出处，错了没有任何地方能发现。
"""

from __future__ import annotations

from typing import Dict, Tuple

import sqlite3

from .registry import register

FROM_VERSION = 20


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    """判断历史库是否已经包含目标表。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _shape(db: sqlite3.Connection) -> Dict[str, str]:
    """读取 facts 表的列名与类型。"""

    rows = db.execute("SELECT * FROM pragma_table_info('facts')").fetchall()
    return {str(row[1]): str(row[2]).upper() for row in rows}


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """为已存在的 facts 补 ``origin_kind``，重放时跳过已有列。

    很早的最小测试库尚无 ``facts`` 表，它会由迁移链尾的当前 DDL 按 v21 完整
    结构创建。自检只覆盖本次真正执行的 ALTER TABLE。
    """

    added: list[Tuple[str, str]] = []
    if _table_exists(db, 'facts') and 'origin_kind' not in _shape(db):
        # 列名、类型与默认值均为静态字面量，不以字符串拼接生成 DDL。
        db.execute(
            "ALTER TABLE facts ADD COLUMN origin_kind TEXT NOT NULL DEFAULT 'legacy'"
        )
        added.append(('facts', 'origin_kind'))

    for table, column in added:
        shape = _shape(db)
        if shape.get(column) != 'TEXT':
            raise RuntimeError(
                f'v21 迁移自检失败：{table}.{column} 类型是 '
                f'{shape.get(column)!r}，预期 TEXT'
            )
