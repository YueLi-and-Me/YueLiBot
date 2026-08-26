"""v17 -> v18：黑话学习侧的三个证据列。

``jargon`` 表此前只出不进：全部词条来自一次性历史迁移，运行时没有任何
INSERT 路径。黑话学习服务上线后需要为每条词条累积「出现证据」并按阶梯
阈值反复推断含义，本迁移补上三个列：

- ``sightings``：学习期累计出现次数（每批语料至多 +1），阶梯阈值判据；
- ``evidence_ids``：证据消息的 ``messages.id`` JSON 数组，推断时取上下文；
- ``inferred_at_sightings``：上次推断时的 ``sightings`` 值，兼作「判定为
  普通词」的标记（大于 0 且 status 仍为 pending 即普通词）。

jargon 表没有历史迁移步骤——它历来由链尾的幂等建表 DDL 创建。因此从老版本
一路走上来的库在本步可能还没有这张表：迁移先保证表存在（缺则按 v18 权威
结构建空表），再补缺失的列，对两种形态都成立。
"""

from __future__ import annotations

import sqlite3

from .registry import register

# v18 的 jargon 权威结构，与 schema.py 的 DDL 同形；老库缺表时按此新建。
_JARGON_DDL = """
CREATE TABLE IF NOT EXISTS jargon (
  id         INTEGER PRIMARY KEY,
  term       TEXT    NOT NULL,
  meaning    TEXT    NOT NULL,
  stream_id  INTEGER REFERENCES streams(id) ON DELETE CASCADE,
  status     TEXT    NOT NULL DEFAULT 'confirmed',
  hits       INTEGER NOT NULL DEFAULT 0,
  source     TEXT    NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  sightings             INTEGER NOT NULL DEFAULT 0,
  evidence_ids          TEXT,
  inferred_at_sightings INTEGER NOT NULL DEFAULT 0,
  UNIQUE(term, stream_id)
);
CREATE INDEX IF NOT EXISTS idx_jargon_status ON jargon(status, stream_id);
"""


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    """判断迁移目标表是否已经存在。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


@register(17)
def migrate(db: sqlite3.Connection) -> None:
    """为 ``jargon`` 表补上学习证据三列，已存在的列跳过。

    重放幂等：缺表则建表、缺列则补列；自检只在本次真的执行了 DDL 时才
    断言——功能上线后重放旧迁移是健康库的常态，无条件断言「新列全默认」
    会把已经累积过证据的库判成损坏。
    """

    if not _table_exists(db, 'jargon'):
        db.executescript(_JARGON_DDL)
        return

    existing = {
        str(row[1]) for row in db.execute("SELECT * FROM pragma_table_info('jargon')")
    }
    added = False
    # ALTER 语句必须是整句静态字面量：列名与类型定义写死在各分支里，
    # 不做任何拼接，与 bootstrap.write_user_version 同一条纪律。
    if 'sightings' not in existing:
        db.execute('ALTER TABLE jargon ADD COLUMN sightings INTEGER NOT NULL DEFAULT 0')
        added = True
    if 'evidence_ids' not in existing:
        db.execute('ALTER TABLE jargon ADD COLUMN evidence_ids TEXT')
        added = True
    if 'inferred_at_sightings' not in existing:
        db.execute(
            'ALTER TABLE jargon ADD COLUMN inferred_at_sightings INTEGER NOT NULL DEFAULT 0')
        added = True

    if not added:
        return

    shape = {
        str(row[1]): str(row[2])
        for row in db.execute("SELECT * FROM pragma_table_info('jargon')")
    }
    expected = {
        'sightings': 'INTEGER',
        'evidence_ids': 'TEXT',
        'inferred_at_sightings': 'INTEGER',
    }
    for name, column_type in expected.items():
        if shape.get(name) != column_type:
            raise RuntimeError(
                f'v18 迁移自检失败：jargon.{name} 类型是 {shape.get(name)!r}，'
                f'预期 {column_type!r}'
            )
