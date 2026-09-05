"""v25 -> v26：人物画像的信任分级——确凿档正文与证据指纹两列。

``person_profile.confirmed`` 存「确凿」档：由 facts 账本直接投影的条目
（JSON 数组，每条带可追溯的 fact id），模型永远不参与生成。
``person_profile.evidence_fingerprint`` 存参与上一轮生成的 fact / episode id
的稳定哈希：置脏后指纹没变就只推进时间戳、清脏位，不再调用模型。

存量行的 ``summary`` 整段保留、语义收窄为「印象」档，确凿档留空，等下一次
自然刷新填上——不给已有数据编造出处（与 origin_kind 回填同一纪律）。
两列同时写进当前 DDL，全新库由建表直接获得；本迁移覆盖的是存量库。
重放幂等：已存在的列跳过，自检只在本次真正执行了 DDL 时才断言。
"""

from __future__ import annotations

from typing import List

import sqlite3

from .registry import register

FROM_VERSION = 25


def _existing_tables(db: sqlite3.Connection) -> set[str]:
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _profile_columns(db: sqlite3.Connection) -> set[str]:
    rows = db.execute("SELECT * FROM pragma_table_info('person_profile')").fetchall()
    return {str(row[1]) for row in rows}


@register(FROM_VERSION)
def migrate(db: sqlite3.Connection) -> None:
    """给 person_profile 补确凿档与证据指纹两列，重放时跳过已有结构。"""

    # 早期最小库可能还没有 person_profile，留待链尾当前 DDL 建表，不猜结构。
    if 'person_profile' not in _existing_tables(db):
        return

    added: List[str] = []
    columns = _profile_columns(db)
    if 'confirmed' not in columns:
        db.execute(
            "ALTER TABLE person_profile ADD COLUMN confirmed TEXT NOT NULL DEFAULT ''"
        )
        added.append('confirmed')
    if 'evidence_fingerprint' not in columns:
        db.execute(
            "ALTER TABLE person_profile ADD COLUMN evidence_fingerprint TEXT NOT NULL DEFAULT ''"
        )
        added.append('evidence_fingerprint')

    if not added:
        return
    missing = {'confirmed', 'evidence_fingerprint'} - _profile_columns(db)
    if missing:
        raise RuntimeError(f'v26 迁移自检失败：person_profile 缺列 {sorted(missing)}')
