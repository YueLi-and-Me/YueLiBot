"""v24 -> v25：N4 反馈纠错的存储表与情节待重建列。

两张新表：``memory_feedback_pending`` 登记「事实真的进了提示词」的待观察锚点，
``memory_feedback_results`` 留存判定成立的纠错结果（含「已被纠正」标记位）。
``episodes.needs_rebuild`` 标记被纠错命中的情节，等待后台重摘要；
待重建期间可以选择屏蔽它的召回。

新表同时写进了当前 DDL，全新库由建表直接获得；本迁移覆盖的是存量库。
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


def _episodes_columns(db: sqlite3.Connection) -> set[str]:
    rows = db.execute("SELECT * FROM pragma_table_info('episodes')").fetchall()
    return {str(row[1]) for row in rows}


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """建两张反馈表并给 episodes 补待重建列，重放时跳过已有结构。"""

    existing = _existing_tables(db)
    added: List[str] = []
    if 'memory_feedback_pending' not in existing:
        db.execute(
            '''CREATE TABLE memory_feedback_pending (
                 id         INTEGER PRIMARY KEY,
                 fact_id    INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
                 person_id  INTEGER NOT NULL,
                 stream_id  INTEGER NOT NULL,
                 entered_at INTEGER NOT NULL,
                 status     TEXT    NOT NULL DEFAULT 'pending',
                 attempts   INTEGER NOT NULL DEFAULT 0,
                 updated_at INTEGER NOT NULL,
                 UNIQUE(fact_id, stream_id)
               )'''
        )
        db.execute(
            'CREATE INDEX idx_feedback_pending_status '
            'ON memory_feedback_pending(status, entered_at)'
        )
        added.append('memory_feedback_pending')
    if 'memory_feedback_results' not in existing:
        db.execute(
            '''CREATE TABLE memory_feedback_results (
                 id                INTEGER PRIMARY KEY,
                 fact_id           INTEGER NOT NULL,
                 person_id         INTEGER NOT NULL,
                 stream_id         INTEGER NOT NULL,
                 confidence        REAL    NOT NULL,
                 corrected_content TEXT    NOT NULL DEFAULT '',
                 new_fact_id       INTEGER NOT NULL DEFAULT 0,
                 marked            INTEGER NOT NULL DEFAULT 0,
                 created_at        INTEGER NOT NULL
               )'''
        )
        db.execute(
            'CREATE INDEX idx_feedback_results_fact '
            'ON memory_feedback_results(fact_id, marked)'
        )
        added.append('memory_feedback_results')

    column_added = False
    # 早期最小库可能还没有 episodes 表，留待链尾当前 DDL 建表，不猜结构。
    if 'episodes' in existing and 'needs_rebuild' not in _episodes_columns(db):
        db.execute('ALTER TABLE episodes ADD COLUMN needs_rebuild INTEGER NOT NULL DEFAULT 0')
        db.execute('CREATE INDEX idx_episodes_rebuild ON episodes(needs_rebuild)')
        column_added = True

    for table in added:
        if table not in _existing_tables(db):
            raise RuntimeError(f'v25 迁移自检失败：{table} 未建成')
    if column_added and 'needs_rebuild' not in _episodes_columns(db):
        raise RuntimeError('v25 迁移自检失败：episodes.needs_rebuild 未建成')
