"""v7 → v8 迁移：把 QQ 群名片从账号昵称中拆成按群保存的属性。"""

from __future__ import annotations

import sqlite3

from .registry import register

from src.common.logger import get_logger

logger = get_logger(__name__)


@register(7)
def v7_to_v8(db: sqlite3.Connection) -> None:
    """创建群成员名片表；历史混合显示名会在后续 QQ 入站时按最新值纠正。"""
    db.execute(
        '''CREATE TABLE group_memberships (
               stream_id  INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
               person_id  INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
               group_card TEXT    NOT NULL,
               updated_at INTEGER NOT NULL,
               PRIMARY KEY(stream_id, person_id)
           )'''
    )
    db.execute(
        '''CREATE INDEX idx_group_memberships_person
           ON group_memberships(person_id)'''
    )
    foreign_key_rows = db.execute('PRAGMA foreign_key_check').fetchall()
    if foreign_key_rows:
        raise RuntimeError('v8 迁移自检失败：外键完整性检查失败')
    logger.info('v7_to_v8_done')
