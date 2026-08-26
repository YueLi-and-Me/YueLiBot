"""v15 -> v16：表达方式表补人工确认标记与最近使用时间。

``checked`` 预留给「只用人工确认过的表达」那道质量闸门，本轮全部置 0 且
读取不按它过滤；``last_used_at`` 记录表达方式最近一次真正进提示词的时间，
与 ``use_count`` 的回写同处发生。两列都允许为空表以外的存量数据直接落默认值，
不触碰任何历史 ``use_count``——那是迁移事实。
"""

from __future__ import annotations

import sqlite3

from .registry import register


def _table_exists(db: sqlite3.Connection) -> bool:
    """判断 expressions 表是否已经存在。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'expressions'"
    ).fetchone()
    return row is not None


def _table_shape(
    db: sqlite3.Connection,
) -> list[tuple[str, str, int, str | None, int]]:
    """按定义顺序读取 expressions 的列名、类型、约束和默认值。"""

    rows = db.execute("SELECT * FROM pragma_table_info('expressions')").fetchall()
    return [
        (str(row[1]), str(row[2]), int(row[3]), row[4], int(row[5]))
        for row in rows
    ]


_EXPECTED_V15 = [
    ('id', 'INTEGER', 0, None, 1),
    ('situation', 'TEXT', 1, None, 0),
    ('style', 'TEXT', 1, None, 0),
    ('stream_id', 'INTEGER', 0, None, 0),
    ('use_count', 'INTEGER', 1, '0', 0),
    ('source', 'TEXT', 1, "''", 0),
    ('created_at', 'INTEGER', 1, None, 0),
]

_EXPECTED_V16 = [
    *_EXPECTED_V15,
    ('checked', 'INTEGER', 1, '0', 0),
    ('last_used_at', 'INTEGER', 0, None, 0),
]


def _migration_needed(db: sqlite3.Connection) -> bool:
    """校验版本入口的表结构，并判断是否需要执行两条 ALTER。"""

    actual = _table_shape(db)
    if actual == _EXPECTED_V15:
        return True
    # 当前 DDL 可能已幂等补齐两列，但旧版本号尚未推进；只有列序、约束和默认值
    # 全部等于 v16 权威结构时，才允许跳过 ALTER 直接接管版本号。
    if actual == _EXPECTED_V16:
        return False
    raise RuntimeError(f'v15 数据库的表达方式表结构不符合预期：expressions={actual}')


@register(15)
def migrate(db: sqlite3.Connection) -> None:
    """为 expressions 增加 checked 与 last_used_at 两列。

    存量行的 ``use_count``、``created_at`` 等历史值逐行对账保持不变；
    新列只落默认值（``checked=0``、``last_used_at=NULL``），不由迁移猜测语义。
    更老的库一路迁上来时这张表还不存在，无需处理——链尾的当前 DDL 会按
    最终形态把它建出来。
    """

    if not _table_exists(db):
        return

    before = db.execute(
        'SELECT id, situation, style, stream_id, use_count, source, created_at'
        ' FROM expressions ORDER BY id'
    ).fetchall()

    if _migration_needed(db):
        db.execute(
            'ALTER TABLE expressions ADD COLUMN checked INTEGER NOT NULL DEFAULT 0'
        )
        db.execute(
            'ALTER TABLE expressions ADD COLUMN last_used_at INTEGER'
        )

    if _table_shape(db) != _EXPECTED_V16:
        raise RuntimeError('v16 迁移自检失败：expressions 结构未到达权威形态')

    after = db.execute(
        'SELECT id, situation, style, stream_id, use_count, source, created_at'
        ' FROM expressions ORDER BY id'
    ).fetchall()
    if after != before:
        raise RuntimeError('v16 迁移自检失败：expressions 原有数据发生变化')

    drift = db.execute(
        'SELECT COUNT(*) FROM expressions WHERE checked != 0 OR last_used_at IS NOT NULL'
    ).fetchone()[0]
    if drift:
        raise RuntimeError('v16 迁移自检失败：新列出现非默认值')
