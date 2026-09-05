"""v28 -> v29：运行画像表，本地采集一台机器自己的安装 ID、启动与运行时长。

本迁移只新建表，不动任何既有表的行。重放幂等：表已存在时整体跳过，
自检只在本次真正建表后断言。

★ **这张表已在 v29 -> v30 删除**，它落地当天依据就作废了（见 v29_to_v30.py）。
当前 DDL 里因此没有它，全新库直接建成 v30 形态、根本不会出现这张表；
本迁移保留在链条里只为让停在 v28 的存量库走完 v28 -> v29 -> v30 这条路。
不要因为「建了又删」就把这两步合并——真机已经越过 v29，合并会让它跳过删表。
"""

from __future__ import annotations

import sqlite3

from .registry import register

FROM_VERSION = 28


def _existing_tables(db: sqlite3.Connection) -> set[str]:
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """为存量库建运行画像表，重放时跳过已有结构。"""

    if 'runtime_profile' in _existing_tables(db):
        return

    db.execute(
        '''CREATE TABLE IF NOT EXISTS runtime_profile (
             id               INTEGER PRIMARY KEY CHECK (id = 1),
             install_id       TEXT    NOT NULL DEFAULT '',
             app_version      TEXT    NOT NULL DEFAULT '',
             launch_count     INTEGER NOT NULL DEFAULT 0,
             first_launch_at  INTEGER,
             last_launch_at   INTEGER,
             total_runtime_ms INTEGER NOT NULL DEFAULT 0,
             updated_at       INTEGER
           )'''
    )

    if 'runtime_profile' not in _existing_tables(db):
        raise RuntimeError('v28 -> v29 迁移自检失败：runtime_profile 未建成')
