"""v13 -> v14：新增连续的生活活动时间线。"""

from __future__ import annotations

import sqlite3

from .registry import register


_ACTIVITIES_DDL = """
CREATE TABLE activities (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  kind           TEXT    NOT NULL,
  doing          TEXT    NOT NULL,
  mood           TEXT    NOT NULL,
  energy_pace    INTEGER NOT NULL,
  mood_pace      INTEGER NOT NULL,
  advances       INTEGER,
  started_at     INTEGER NOT NULL,
  expected_until INTEGER NOT NULL,
  ended_at       INTEGER,
  source         TEXT    NOT NULL
);
CREATE INDEX idx_activities_time ON activities(started_at DESC);
"""


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    """判断迁移目标表是否已经存在。"""

    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_shape(
    db: sqlite3.Connection,
) -> list[tuple[str, str, int, str | None, int]]:
    """读取活动表的列定义，供幂等迁移入口严格核对。"""

    rows = db.execute("SELECT * FROM pragma_table_info('activities')").fetchall()
    return [
        (str(row[1]), str(row[2]), int(row[3]), row[4], int(row[5]))
        for row in rows
    ]


def _legacy_plans(db: sqlite3.Connection) -> list[tuple[object, ...]]:
    """逐字读取所有旧日程键，迁移前后用于无损对账。"""

    if not _table_exists(db, 'meta'):
        return []
    return db.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'day_plan:%' ORDER BY key"
    ).fetchall()


def _assert_current_shape(db: sqlite3.Connection) -> None:
    """确认已存在的活动表与 v14 权威结构完全一致。"""

    expected = [
        ('id', 'INTEGER', 0, None, 1),
        ('kind', 'TEXT', 1, None, 0),
        ('doing', 'TEXT', 1, None, 0),
        ('mood', 'TEXT', 1, None, 0),
        ('energy_pace', 'INTEGER', 1, None, 0),
        ('mood_pace', 'INTEGER', 1, None, 0),
        ('advances', 'INTEGER', 0, None, 0),
        ('started_at', 'INTEGER', 1, None, 0),
        ('expected_until', 'INTEGER', 1, None, 0),
        ('ended_at', 'INTEGER', 0, None, 0),
        ('source', 'TEXT', 1, None, 0),
    ]
    actual = _table_shape(db)
    if actual != expected:
        raise RuntimeError(f'v14 活动表结构不符合预期：activities={actual}')


@register(13)
def migrate(db: sqlite3.Connection) -> None:
    """创建空活动表，并原样保留所有旧格式日程元数据。

    旧 ``day_plan:*`` 只表达当时计划过什么，不是实际做过什么。迁移因此明确选择
    留着但不读，也绝不依据旧 ``slots`` 合成活动；这样既保留审计材料，又不会把
    历史计划伪造成生活记忆。当日计划由新运行时按新结构重新生成。
    """

    plans_before = _legacy_plans(db)
    existed = _table_exists(db, 'activities')
    if existed:
        _assert_current_shape(db)
    else:
        db.executescript(_ACTIVITIES_DDL)

    plans_after = _legacy_plans(db)
    if plans_after != plans_before:
        raise RuntimeError('v14 迁移自检失败：旧 day_plan 元数据发生变化')

    # v13 的权威结构没有活动表；新建后的首个事实必须由运行时产生，而不是迁移猜测。
    if not existed:
        count = db.execute('SELECT COUNT(*) FROM activities').fetchone()[0]
        if count != 0:
            raise RuntimeError('v14 迁移自检失败：新活动表不是空表')
