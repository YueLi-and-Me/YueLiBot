"""v14 -> v15：新增会话高频词表。"""

from __future__ import annotations

import sqlite3

from .registry import register


_HIGH_FREQUENCY_DDL = """
CREATE TABLE IF NOT EXISTS high_frequency_terms (
  stream_id        INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
  term             TEXT    NOT NULL,
  occurrence_count INTEGER NOT NULL,
  message_count    INTEGER NOT NULL,
  rank             INTEGER NOT NULL,
  built_at         INTEGER NOT NULL,
  PRIMARY KEY (stream_id, term)
);
"""


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    """判断迁移目标表是否已经存在。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


@register(14)
def migrate(db: sqlite3.Connection) -> None:
    """创建空的会话高频词表。

    高频词表是后台统计任务的快照产物，首个版本必须由运行时从 ``messages``
    统计得出，而不是迁移从旧数据猜测——没有旧数据可猜，也不该有。
    """

    existed = _table_exists(db, 'high_frequency_terms')
    if existed:
        # 幂等重放：表结构必须与 v15 权威结构一致，否则拒绝继续。
        rows = db.execute(
            "SELECT * FROM pragma_table_info('high_frequency_terms')"
        ).fetchall()
        shape = [(str(row[1]), str(row[2])) for row in rows]
        expected = [
            ('stream_id', 'INTEGER'),
            ('term', 'TEXT'),
            ('occurrence_count', 'INTEGER'),
            ('message_count', 'INTEGER'),
            ('rank', 'INTEGER'),
            ('built_at', 'INTEGER'),
        ]
        if shape != expected:
            raise RuntimeError(f'v15 高频词表结构不符合预期：{shape}')
    else:
        db.executescript(_HIGH_FREQUENCY_DDL)

    if not existed:
        count = db.execute('SELECT COUNT(*) FROM high_frequency_terms').fetchone()[0]
        if count != 0:
            raise RuntimeError('v15 迁移自检失败：新建的高频词表不是空表')
