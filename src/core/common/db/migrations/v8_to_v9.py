"""执行数据库结构版本 8 到版本 9 的迁移。

版本 8 创建 ``group_memberships`` 表时只建表、未回填历史数据，成员关系完全依赖后续
入站消息逐条补写。该策略只覆盖此后仍会发言的人物：在版本 8 之前于群聊发过言、之后
再未出现的人物永远拿不到成员关系行，而观察快照按「群聊发言者必然存在成员关系」的
不变量读取，遇到这类人物会直接抛错并使整个快照接口返回 500。本迁移按历史 user 消息
补齐缺失的成员关系，使该不变量对全部历史数据成立；迁移注册表负责按数据库版本调用
本函数。
"""

from __future__ import annotations

import sqlite3

from .registry import register

from src.core.common.logger import get_logger

logger = get_logger(__name__)


@register(8)
def v8_to_v9(db: sqlite3.Connection) -> None:
    """按历史群聊发言记录补齐缺失的群成员关系。

    :param db: 当前迁移事务使用的 SQLite 连接。

    :raises RuntimeError: 回填后仍存在没有成员关系的群聊发言者。
    :raises sqlite3.Error: 成员关系写入或自检查询失败。

    副作用：
        向 ``group_memberships`` 插入缺失行；已存在的行连同其群名片原样保留，
        不提交事务。
    """
    # 群名片属于 person-stream 关系，历史消息中没有留存，只能写空串——该值在本表中
    # 本就表示「未设置群名片」，与真实的空名片同义，不会伪造出一个不存在的名片。
    # updated_at 取该人在该群的最后一条发言时间：这是成员关系最后一次被确凿观察到
    # 的时刻，取当前时间反而会谎称这条关系刚刚被平台确认过。
    # 查询语句保持整句字面量：安全门禁不接受任何动态构造的 SQL 文本；回填与自检
    # 两处的判定条件必须逐字一致，自检才有意义。
    missing = db.execute('''
        SELECT m.stream_id AS stream_id, m.sender_person_id AS person_id,
               MAX(m.created_at) AS last_spoke_at
          FROM messages AS m
          JOIN streams AS s ON s.id = m.stream_id
          JOIN persons AS p ON p.id = m.sender_person_id
         WHERE s.kind = 'group'
           AND m.role = 'user'
           AND NOT EXISTS (
               SELECT 1 FROM group_memberships AS gm
                WHERE gm.stream_id = m.stream_id
                  AND gm.person_id = m.sender_person_id
           )
         GROUP BY m.stream_id, m.sender_person_id
    ''').fetchall()
    if missing:
        db.executemany(
            'INSERT INTO group_memberships (stream_id, person_id, group_card, updated_at) '
            'VALUES (?, ?, ?, ?)',
            [(stream_id, person_id, '', last_spoke_at)
             for stream_id, person_id, last_spoke_at in missing],
        )
    inserted = len(missing)

    remaining = len(db.execute('''
        SELECT m.stream_id AS stream_id, m.sender_person_id AS person_id,
               MAX(m.created_at) AS last_spoke_at
          FROM messages AS m
          JOIN streams AS s ON s.id = m.stream_id
          JOIN persons AS p ON p.id = m.sender_person_id
         WHERE s.kind = 'group'
           AND m.role = 'user'
           AND NOT EXISTS (
               SELECT 1 FROM group_memberships AS gm
                WHERE gm.stream_id = m.stream_id
                  AND gm.person_id = m.sender_person_id
           )
         GROUP BY m.stream_id, m.sender_person_id
    ''').fetchall())
    if remaining:
        raise RuntimeError(f'v9 迁移自检失败：仍有 {remaining} 个群聊发言者缺少成员关系')

    foreign_key_rows = db.execute('PRAGMA foreign_key_check').fetchall()
    if foreign_key_rows:
        raise RuntimeError('v9 迁移自检失败：外键完整性检查失败')

    logger.info('v8_to_v9_done', backfilled=inserted)
