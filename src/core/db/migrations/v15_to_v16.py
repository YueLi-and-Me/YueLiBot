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


def _matches_history(
    actual: list[tuple[str, str, int, str | None, int]],
    expected: list[tuple[str, str, int, str | None, int]],
) -> bool:
    """判断实际列结构是否以某个历史权威结构开头。

    :param actual: PRAGMA 读出的当前列结构，按定义顺序。
    :param expected: 目标历史版本的权威列结构。
    :return: 实际结构前 len(expected) 列的列序、类型、约束与默认值全部一致时返回
        True；实际结构比 expected 短时返回 False。
    副作用：无。
    """

    return actual[:len(expected)] == expected


def _migration_needed(db: sqlite3.Connection) -> bool:
    """校验版本入口的表结构，并判断是否需要执行两条 ALTER。

    [关键] 用前缀比较而不是整表相等。

    - 现象：链尾 DDL 建出的库里 expressions 已带后续版本追加的列，链条从旧入口补跑
      时这里会因「多出列」判结构不符，把正常库拦成迁移损坏。
    - 原因：版本入口可能在当前 DDL 建好后回退 ``user_version`` 得到（迁移演练与
      测试构造都是这条路径），此时列结构天然是「历史权威结构 + 后续版本的列」；
      整表相等只对恰好停在 v16 的历史库成立。
    - 后果：每给 expressions 加一列都要回头改这条旧迁移，链尾 DDL 与历史断言互相
      锁死，迁移演练无法从旧版本号验证加列逻辑。
    """

    actual = _table_shape(db)
    # 必须先比 v16：v16 结构以 v15 为前缀，先比 v15 会把 v16 形状的库误判成需要补列。
    if _matches_history(actual, _EXPECTED_V16):
        return False
    if _matches_history(actual, _EXPECTED_V15):
        return True
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

    columns_added = _migration_needed(db)
    if columns_added:
        db.execute(
            'ALTER TABLE expressions ADD COLUMN checked INTEGER NOT NULL DEFAULT 0'
        )
        db.execute(
            'ALTER TABLE expressions ADD COLUMN last_used_at INTEGER'
        )

    if not _matches_history(_table_shape(db), _EXPECTED_V16):
        raise RuntimeError('v16 迁移自检失败：expressions 结构未到达权威形态')

    after = db.execute(
        'SELECT id, situation, style, stream_id, use_count, source, created_at'
        ' FROM expressions ORDER BY id'
    ).fetchall()
    if after != before:
        raise RuntimeError('v16 迁移自检失败：expressions 原有数据发生变化')

    # 只在本次真的执行了 ALTER 时才断言新列全默认。
    #
    # - 现象：链条重放（例如手工把 user_version 回退后重跑）时，这条自检会因为
    #   「新列出现非默认值」失败，而库其实是好的。
    # - 原因：功能上线后回写路径会往 checked / last_used_at 写入真实使用记录，
    #   此时两列有值是正确数据，不是迁移写坏的痕迹。
    # - 后果：无条件断言会把「已经在用的库」误判成损坏，迁移直接中止且无法自愈。
    if columns_added:
        drift = db.execute(
            'SELECT COUNT(*) FROM expressions WHERE checked != 0 OR last_used_at IS NOT NULL'
        ).fetchone()[0]
        if drift:
            raise RuntimeError('v16 迁移自检失败：新增的两列出现非默认值')
