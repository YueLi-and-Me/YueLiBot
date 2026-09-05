"""v11 -> v12：为消息记录补充平台原生消息编号。"""

from __future__ import annotations

import sqlite3

from .registry import register


@register(11)
def migrate(db: sqlite3.Connection) -> None:
    """新增 ``external_message_id`` 与查询索引。

    出站引用回复要把决策选中的内部消息 ID 还原成平台编号，历史消息不在缓冲区里，
    只能从库里查。历史记录没有留过平台编号，保持 NULL：这些旧消息无法被引用，
    但不影响新消息。
    """

    columns = {
        str(row[1])
        for row in db.execute("PRAGMA table_info('messages')").fetchall()
    }
    if 'external_message_id' not in columns:
        db.execute('ALTER TABLE messages ADD COLUMN external_message_id TEXT')
    db.execute(
        'CREATE INDEX IF NOT EXISTS idx_messages_stream_external '
        'ON messages(stream_id, external_message_id)'
    )
