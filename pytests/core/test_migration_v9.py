"""验证 v8→v9 迁移为历史群聊发言者补齐群成员关系。

版本 8 建 ``group_memberships`` 表时未回填，成员关系只靠后续入站消息补写，因此在
版本 8 之前发过言、之后不再出现的人物永远缺行，观察快照读取时会直接抛错。本模块
构造该缺口并验证迁移把不变量恢复为「群聊 user 消息的发送者必有成员关系行」，同时
不改动已有行的群名片。
"""

from __future__ import annotations

from pathlib import Path

import sqlite3

from src.core.common.db.migrations.manager import CURRENT_VERSION, run_migrations


# 缺口构造中使用的固定时间戳，便于断言 updated_at 取的是最后一条发言时间。
SILENT_FIRST_AT = 1_700_000_000_000
SILENT_LAST_AT = 1_700_000_500_000
ACTIVE_AT = 1_700_009_000_000
CARD_UPDATED_AT = 1_700_008_000_000


def _build_v8_database_with_gap(path: Path) -> None:
    """构造一个停在版本 8、且存在成员关系缺口的数据库。

    :param path: 目标数据库文件路径。

    :return: ``None``。

    副作用：
        先按当前 schema 初始化数据库，再写入群聊会话、人物、消息与仅覆盖部分人物的
        成员关系，最后把 ``user_version`` 回退到 8 以模拟版本 8 的历史状态。
    """
    db = sqlite3.connect(str(path))
    run_migrations(db, path)

    db.execute(
        "INSERT INTO streams (id, platform, kind, external_id) VALUES (2, 'qq', 'group', '629201002')"
    )
    db.execute(
        "INSERT INTO streams (id, platform, kind, external_id) VALUES (3, 'qq', 'direct', '900000001')"
    )
    for person_id, first_seen_at in ((2, SILENT_FIRST_AT), (3, ACTIVE_AT), (4, ACTIVE_AT)):
        db.execute(
            "INSERT INTO persons (id, kind, first_seen_at) VALUES (?, 'contact', ?)",
            (person_id, first_seen_at),
        )

    # person 2 在群里发过两条又销声匿迹，person 3 仍活跃，person 4 只在私聊出现。
    messages = [
        ('user', '早', SILENT_FIRST_AT, 2, 2),
        ('user', '走了', SILENT_LAST_AT, 2, 2),
        ('user', '在吗', ACTIVE_AT, 2, 3),
        ('assistant', '在的', ACTIVE_AT, 2, None),
        ('user', '私聊你', ACTIVE_AT, 3, 4),
    ]
    db.executemany(
        '''INSERT INTO messages (role, content, created_at, stream_id, sender_person_id)
           VALUES (?, ?, ?, ?, ?)''',
        messages,
    )

    # 只有仍活跃的 person 3 拿到了成员关系，且带着真实群名片。
    db.execute(
        '''INSERT INTO group_memberships (stream_id, person_id, group_card, updated_at)
           VALUES (2, 3, '群里的老朋友', ?)''',
        (CARD_UPDATED_AT,),
    )

    db.execute('PRAGMA user_version = 8')
    db.commit()
    db.close()


def test_v8_到_v9_补齐历史群聊发言者的成员关系(tmp_path: Path) -> None:
    """缺行的群聊发言者被补齐，已有成员关系与群名片保持不变。"""
    path = tmp_path / 'memory.db'
    _build_v8_database_with_gap(path)

    db = sqlite3.connect(str(path))
    run_migrations(db, path)

    assert db.execute('PRAGMA user_version').fetchone()[0] == CURRENT_VERSION

    rows = db.execute(
        'SELECT stream_id, person_id, group_card, updated_at FROM group_memberships '
        'ORDER BY stream_id, person_id'
    ).fetchall()
    assert rows == [
        # 群名片无法从历史消息还原，补空串（本表中即「未设置群名片」）；
        # updated_at 取最后一条发言时间，而不是迁移执行时间。
        (2, 2, '', SILENT_LAST_AT),
        (2, 3, '群里的老朋友', CARD_UPDATED_AT),
    ]
    db.close()


def test_v9_迁移后群聊发言者不再缺少成员关系(tmp_path: Path) -> None:
    """迁移后不变量对全部历史数据成立，快照读取不会再遇到缺行。"""
    path = tmp_path / 'memory.db'
    _build_v8_database_with_gap(path)

    db = sqlite3.connect(str(path))
    run_migrations(db, path)

    missing = db.execute(
        '''SELECT COUNT(*) FROM (
               SELECT DISTINCT m.stream_id, m.sender_person_id
                 FROM messages AS m
                 JOIN streams AS s ON s.id = m.stream_id
                 JOIN persons AS p ON p.id = m.sender_person_id
                WHERE s.kind = 'group' AND m.role = 'user'
                  AND NOT EXISTS (
                      SELECT 1 FROM group_memberships AS gm
                       WHERE gm.stream_id = m.stream_id AND gm.person_id = m.sender_person_id
                  )
           )'''
    ).fetchone()[0]
    assert missing == 0
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    db.close()
