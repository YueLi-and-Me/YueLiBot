"""v10 -> v11：为表情包记录补充 OneBot 图片子类型。"""

from __future__ import annotations

import sqlite3

from .registry import register


@register(10)
def migrate(db: sqlite3.Connection) -> None:
    """新增 ``sub_type``，历史及本地导入记录按 QQ 表情包类型 ``1`` 补齐。"""

    columns = {
        str(row[1])
        for row in db.execute("PRAGMA table_info('emoji')").fetchall()
    }
    if 'sub_type' in columns:
        return
    db.execute(
        'ALTER TABLE emoji ADD COLUMN sub_type INTEGER NOT NULL DEFAULT 1'
    )
