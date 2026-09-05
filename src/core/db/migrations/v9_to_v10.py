"""v9 -> v10：新增可校验、可发送的表情包库。"""

from __future__ import annotations

import sqlite3

from .registry import register


@register(9)
def migrate(db: sqlite3.Connection) -> None:
    """创建以内容 SHA-256 去重的最小表情包表。"""

    db.execute(
        '''CREATE TABLE IF NOT EXISTS emoji (
               hash          TEXT PRIMARY KEY,
               send_ref      TEXT NOT NULL,
               emotion_tags  TEXT NOT NULL,
               emotion_vec   BLOB,
               seen_count    INTEGER NOT NULL DEFAULT 1,
               first_seen_at INTEGER NOT NULL
           )'''
    )
