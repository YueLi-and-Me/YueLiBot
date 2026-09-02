"""对比库内实际结构与当前 DDL 期望结构，并渲染成启动时的控制台报告。

存在的理由是 ``CREATE TABLE IF NOT EXISTS`` 的两个盲区：

1. 建表静默：新增一张表时，启动日志不留任何痕迹，无法确认表是否建立。
2. 加列无效且不报错：表已存在时该语句直接跳过，DDL 新增的列不会被补上，
   库与代码就此漂移；读旧列的查询照常工作，直到写新列的语句在运行期失败。

因此本模块把「期望结构」与「实际结构」都物化成 表名 → 列名集合 的字典再做差集：
期望结构由当前 DDL 在内存库里跑一遍得到，不靠解析 SQL 文本。

对外暴露 :func:`describe_schema_changes`（算差异）与 :func:`report_schema_changes`
（差异非空时打印信息框并记日志），由 ``migrations.manager`` 在 schema 落地前后调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import sqlite3

from src.core.common.console_layout import print_box
from src.core.common.logger import get_logger

from .schema import DDL

logger = get_logger(__name__)

SchemaShape = Dict[str, Set[str]]


@dataclass
class SchemaChanges:
    """一次 schema 落地前后的结构差异。

    :ivar created: 本次新建的表名，按字母序。
    :ivar drifted: 表名 → 缺失列名列表；DDL 声明了但库里没有，只可能由加列的
        ``CREATE TABLE IF NOT EXISTS`` 无效导致，需要一支迁移来补。
    """

    created: List[str] = field(default_factory=list)
    added_columns: Dict[str, List[str]] = field(default_factory=dict)
    drifted: Dict[str, List[str]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """判断是否既没有新建表、也没有新增列或列漂移。"""

        return not self.created and not self.added_columns and not self.drifted


def _shape(db: sqlite3.Connection) -> SchemaShape:
    """读取一个连接里所有业务表的表名与列名。

    :param db: 已打开的 SQLite 连接。
    :return: 表名到列名集合的映射；``sqlite_`` 前缀的内部表与 FTS 影子表一律排除，
        它们由 FTS5 自行维护，不属于本项目声明的结构。
    :raises sqlite3.Error: 查询 ``sqlite_master`` 或 ``pragma table_info`` 失败。
    副作用：只读，不修改任何表。
    """

    shape: SchemaShape = {}
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    # FTS5 会为每张虚拟表自动建 _data / _idx / _docsize / _config 影子表。它们不是
    # 本项目声明的结构，列进报告只会把真正新增的业务表淹掉，因此按虚拟表名前缀剔除。
    virtual = {
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL TABLE%'"
        )
    }
    shadows = {
        name for (name,) in rows
        if any(name.startswith(f'{vt}_') for vt in virtual)
    }
    for (name,) in rows:
        if name in shadows:
            continue
        # PRAGMA 不支持参数绑定，表名只能拼进语句。名字取自 sqlite_master，是库
        # 自己的目录而非外部输入，因此不构成注入面；但仍按 SQLite 标识符规则把内嵌
        # 双引号翻倍——库里若真存在用引号构造的表名，不翻倍会拼出越界语句，而且
        # 静态扫描每次都会把这行报成注入点，翻倍一次即可两边都免。
        quoted = name.replace('"', '""')
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{quoted}")')}
        shape[name] = columns
    return shape


def _expected_shape() -> SchemaShape:
    """在内存库里跑一遍当前 DDL，得到代码期望的结构。

    :return: 表名到列名集合的映射。
    :raises sqlite3.Error: 当前 DDL 无法执行：属于代码缺陷，不应静默忽略。
    副作用：创建并丢弃一个内存数据库，不触碰真实库。
    """

    probe = sqlite3.connect(':memory:')
    try:
        probe.executescript(DDL)
        return _shape(probe)
    finally:
        probe.close()


def describe_schema_changes(before: SchemaShape, after: SchemaShape) -> SchemaChanges:
    """比较落地前后的实际结构，并与 DDL 期望结构对账。

    :param before: 执行 DDL 之前的实际结构。
    :param after: 执行 DDL 之后的实际结构。
    :return: 新建表清单与列漂移清单。
    :raises sqlite3.Error: 计算期望结构时 DDL 执行失败。
    副作用：无。
    """

    expected = _expected_shape()
    created = sorted(set(after) - set(before))
    # 已存在的表上多出来的列只可能来自迁移的 ALTER TABLE；逐字段报出来，
    # 「这一版给某张表加了什么」才不用去翻迁移源码。
    added_columns = {
        table: sorted(columns - before[table])
        for table, columns in after.items()
        if table in before and columns - before[table]
    }
    drifted: Dict[str, List[str]] = {}
    for table, columns in expected.items():
        missing = sorted(columns - after.get(table, set()))
        # 表本身不存在时不算漂移：那属于「该建没建」，会由上面的 created 或
        # 更上层的执行失败暴露；这里只盯「表在、列不在」这一种沉默故障。
        if missing and table in after:
            drifted[table] = missing
    return SchemaChanges(created=created, added_columns=added_columns, drifted=drifted)


def _fact_kind_distribution(db: sqlite3.Connection) -> List[Tuple[str, int]]:
    """读取事实类别分布，按数量降序、类别升序返回。"""

    rows = db.execute(
        '''SELECT kind, COUNT(*) AS amount
           FROM facts
           GROUP BY kind
           ORDER BY amount DESC, kind ASC'''
    ).fetchall()
    return [(str(kind), int(amount)) for kind, amount in rows]


def _fact_ledger_counts(db: sqlite3.Connection) -> Tuple[int, int]:
    """读取事实账本计数：带槽位的事实条数、已被取代的事实条数。"""

    slotted = db.execute("SELECT COUNT(*) FROM facts WHERE slot <> ''").fetchone()[0]
    superseded = db.execute(
        'SELECT COUNT(*) FROM facts WHERE superseded_by IS NOT NULL'
    ).fetchone()[0]
    return int(slotted), int(superseded)


def report_schema_changes(
    changes: SchemaChanges,
    version: int,
    db: Optional[sqlite3.Connection] = None,
) -> None:
    """把结构变化打印成控制台信息框并写入日志。

    无变化时不打印：每次启动都输出「无变化」的框会使真正有变化的那次被淹没。

    :param changes: :func:`describe_schema_changes` 的产物。
    :param version: 当前 ``user_version``，一并展示便于与迁移记录对账。
    :param db: 可选当前数据库连接；提供时额外输出事实类别分布。
    :return: 无返回值。
    副作用：向 stdout 打印信息框，并记一条 info 或 error 日志。
    """

    kind_line: Optional[str] = None
    ledger_counts: Optional[Tuple[int, int]] = None
    if db is not None:
        distribution = _fact_kind_distribution(db)
        kind_line = '、'.join(f'{kind} {amount}' for kind, amount in distribution) or '暂无事实'
        logger.info(
            'db_fact_kind_distribution',
            distribution=kind_line,
            total=sum(amount for _, amount in distribution),
        )
        ledger_counts = _fact_ledger_counts(db)
    if changes.is_empty():
        return
    rows: List[str] = [f'schema 版本：{version}']
    if kind_line is not None:
        rows.append(f'事实 kind 分布：{kind_line}')
    if ledger_counts is not None:
        rows.append(f'带槽位的事实：{ledger_counts[0]}')
        rows.append(f'已被取代的事实：{ledger_counts[1]}')
    if changes.created:
        rows.append(f'本次新建的表（{len(changes.created)}）：')
        rows.extend(f'  + {name}' for name in changes.created)
    if changes.added_columns:
        rows.append('')
        rows.append('本次新增的字段：')
        for table, columns in sorted(changes.added_columns.items()):
            for column in columns:
                rows.append(f'  + {table}.{column}')
    if changes.drifted:
        rows.append('')
        rows.append('⚠ 库与 DDL 不一致——下列列在代码里已声明，但库里没有：')
        for table, columns in sorted(changes.drifted.items()):
            rows.append(f'  ! {table}：缺 {"、".join(columns)}')
        rows.append('')
        rows.append('原因：CREATE TABLE IF NOT EXISTS 对已存在的表不加列，也不报错。')
        rows.append('处置：为这些列写一支迁移；改 DDL 本身不会补上它们。')
    print_box('数据库结构变更', rows, width=96, source=__name__)
    if changes.drifted:
        logger.error('db_schema_drift', tables=sorted(changes.drifted), version=version)
    else:
        logger.info(
            'db_schema_applied',
            tables=changes.created,
            columns=[f'{t}.{c}' for t, cs in sorted(changes.added_columns.items()) for c in cs],
            version=version,
        )


def snapshot_shape(db: sqlite3.Connection) -> SchemaShape:
    """对外暴露的结构快照入口，供 schema 落地前后各取一次。

    :param db: 已打开的 SQLite 连接。
    :return: 表名到列名集合的映射。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """

    return _shape(db)
