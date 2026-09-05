"""v12 -> v13：为自身状态与历史快照增加心情轴。"""

from __future__ import annotations

import sqlite3

from .registry import register


def _table_shape(
    db: sqlite3.Connection,
    table: str,
) -> list[tuple[str, str, int, str | None, int]]:
    """按定义顺序读取固定迁移目标表的列名、类型、约束和默认值。"""

    rows = db.execute('SELECT * FROM pragma_table_info(?)', (table,)).fetchall()
    return [
        (str(row[1]), str(row[2]), int(row[3]), row[4], int(row[5]))
        for row in rows
    ]


def _migration_needed(db: sqlite3.Connection) -> bool:
    """校验版本入口的表结构，并判断是否需要执行两条 ALTER。"""

    expected_v12: dict[str, list[tuple[str, str, int, str | None, int]]] = {
        'persona_self': [
            ('id', 'INTEGER', 0, None, 1),
            ('energy', 'REAL', 1, None, 0),
            ('updated_at', 'INTEGER', 1, None, 0),
        ],
        'persona_snapshots': [
            ('date', 'TEXT', 0, None, 1),
            ('intimacy', 'REAL', 1, None, 0),
            ('energy', 'REAL', 1, None, 0),
            ('captured_at', 'INTEGER', 1, None, 0),
        ],
    }
    actual = {
        table: _table_shape(db, table)
        for table in expected_v12
    }
    if actual == expected_v12:
        return True

    # 当前 DDL 可能已幂等创建好 mood，但旧版本号尚未推进；只有列序、约束和默认值
    # 全部等于 v13 权威结构时，才允许进入后续的数据完整性检查并接管版本号。
    expected_v13 = {
        'persona_self': [
            *expected_v12['persona_self'],
            ('mood', 'REAL', 1, '50.0', 0),
        ],
        'persona_snapshots': [
            *expected_v12['persona_snapshots'],
            ('mood', 'REAL', 1, '50.0', 0),
        ],
    }
    if actual == expected_v13:
        return False

    raise RuntimeError(
        'v12 数据库的人格状态结构不符合预期：'
        f"persona_self={actual['persona_self']}，"
        f"persona_snapshots={actual['persona_snapshots']}"
    )


def _assert_migration_integrity(
    db: sqlite3.Connection,
    before_self: list[tuple[object, ...]],
    before_snapshots: list[tuple[object, ...]],
) -> None:
    """确认旧字段逐行不变，且历史记录的心情统一回填为中性值。"""

    after_self = db.execute(
        'SELECT id, energy, updated_at FROM persona_self ORDER BY id'
    ).fetchall()
    if after_self != before_self:
        raise RuntimeError('v13 迁移自检失败：persona_self 原有数据发生变化')

    after_snapshots = db.execute(
        '''SELECT date, intimacy, energy, captured_at
           FROM persona_snapshots ORDER BY date'''
    ).fetchall()
    if after_snapshots != before_snapshots:
        raise RuntimeError('v13 迁移自检失败：persona_snapshots 原有数据发生变化')

    for table in ('persona_self', 'persona_snapshots'):
        invalid = db.execute(
            f'SELECT 1 FROM {table} WHERE mood IS NULL OR mood != 50.0 LIMIT 1'
        ).fetchone()
        if invalid is not None:
            raise RuntimeError(f'v13 迁移自检失败：{table}.mood 未统一回填 50')


@register(12)
def migrate(db: sqlite3.Connection) -> None:
    """新增心情字段，并保留 v12 的精力、关系和时间数据。"""

    migration_needed = _migration_needed(db)
    before_self = db.execute(
        'SELECT id, energy, updated_at FROM persona_self ORDER BY id'
    ).fetchall()
    before_snapshots = db.execute(
        '''SELECT date, intimacy, energy, captured_at
           FROM persona_snapshots ORDER BY date'''
    ).fetchall()
    if not migration_needed:
        _assert_migration_integrity(db, before_self, before_snapshots)
        return

    db.execute(
        'ALTER TABLE persona_self ADD COLUMN mood REAL NOT NULL DEFAULT 50.0'
    )
    db.execute(
        'ALTER TABLE persona_snapshots ADD COLUMN mood REAL NOT NULL DEFAULT 50.0'
    )

    _assert_migration_integrity(db, before_self, before_snapshots)
