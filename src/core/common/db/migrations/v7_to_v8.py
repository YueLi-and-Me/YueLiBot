"""执行数据库结构版本 7 到版本 8 的迁移。

本迁移创建按会话和人物保存 QQ 群名片的关联表，使群名片与账号昵称分离；迁移注册表
负责按数据库版本调用本函数，历史混合显示名由后续入站事件逐步校正。
"""

from __future__ import annotations

import sqlite3

from .registry import register

from src.core.common.logger import get_logger

logger = get_logger(__name__)


@register(7)
def v7_to_v8(db: sqlite3.Connection) -> None:
    """创建按群聊 stream 和人物保存群成员名片的关联表。

    Args:
        db: 当前迁移事务使用的 SQLite 连接。

    Raises:
        RuntimeError: 迁移后外键完整性检查失败。
        sqlite3.Error: 关联表或索引创建失败。

    Side Effects:
        创建 ``group_memberships`` 表及人物索引，并执行外键检查；不提交事务。
    """
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
