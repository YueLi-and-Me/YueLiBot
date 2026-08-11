"""执行数据库结构版本 6 到版本 7 的迁移。

本迁移将旧关系状态字段收敛为按人物保存的单一好感度，并通过迁移注册表在启动时按
版本顺序执行；SQLite 连接和事务由迁移管理器提供。
"""

from __future__ import annotations

import sqlite3

from .registry import register

from src.common.logger import get_logger

logger = get_logger(__name__)


def _table_columns(db: sqlite3.Connection, table: str) -> list[str]:
    """按 SQLite 定义顺序读取表列名。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :param table: 要检查的表名，必须来自固定迁移 SQL。
    :return: 按列序排列的列名列表。
    :raises sqlite3.Error: 表结构查询失败时抛出。
    :side_effects: 只读 SQLite 表结构。
    """
    return [str(row[1]) for row in db.execute(f'PRAGMA table_info({table})').fetchall()]


def _rebuild_persona_bond(db: sqlite3.Connection) -> tuple[int, int]:
    """重建人格关系表并移除已废弃的关系维度。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :return: `(迁移前行数, 迁移后行数)`。
    :raises RuntimeError: 旧表列结构不符合 v6 预期。
    :raises sqlite3.Error: 表创建、复制或替换失败。
    :side_effects: 替换 `persona_bond` 表，不提交事务。
    """
    columns = _table_columns(db, 'persona_bond')
    expected = ['person_id', 'intimacy', 'tsundere', 'reliance', 'updated_at']
    if columns != expected:
        raise RuntimeError(
            f'v6 数据库的 persona_bond 结构不符合预期：{columns}'
        )

    before = int(db.execute('SELECT COUNT(*) FROM persona_bond').fetchone()[0])
    db.execute(
        '''CREATE TABLE persona_bond_v7 (
               person_id  INTEGER PRIMARY KEY REFERENCES persons(id),
               intimacy   REAL    NOT NULL,
               updated_at INTEGER NOT NULL
           )'''
    )
    db.execute(
        '''INSERT INTO persona_bond_v7 (person_id, intimacy, updated_at)
           SELECT person_id, intimacy, updated_at FROM persona_bond'''
    )
    db.execute('DROP TABLE persona_bond')
    db.execute('ALTER TABLE persona_bond_v7 RENAME TO persona_bond')
    after = int(db.execute('SELECT COUNT(*) FROM persona_bond').fetchone()[0])
    return before, after


def _rebuild_persona_snapshots(db: sqlite3.Connection) -> tuple[int, int]:
    """重建人格快照表并保留 intimacy、energy 与时间字段。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :return: `(迁移前行数, 迁移后行数)`。
    :raises RuntimeError: 旧表列结构不符合 v6 预期。
    :raises sqlite3.Error: 表创建、复制、替换或索引创建失败。
    :side_effects: 替换 `persona_snapshots` 表，不提交事务。
    """
    # 只允许从已知 v6 列结构迁移，防止在未知 schema 上误删数据。
    columns = _table_columns(db, 'persona_snapshots')
    expected = ['date', 'intimacy', 'tsundere', 'reliance', 'energy', 'captured_at']
    if columns != expected:
        raise RuntimeError(
            f'v6 数据库的 persona_snapshots 结构不符合预期：{columns}'
        )

    before = int(db.execute('SELECT COUNT(*) FROM persona_snapshots').fetchone()[0])
    # 复制保留字段并通过行数校验确认快照未丢失。
    db.execute(
        '''CREATE TABLE persona_snapshots_v7 (
               date        TEXT PRIMARY KEY,
               intimacy    REAL    NOT NULL,
               energy      REAL    NOT NULL,
               captured_at INTEGER NOT NULL
           )'''
    )
    db.execute(
        '''INSERT INTO persona_snapshots_v7 (date, intimacy, energy, captured_at)
           SELECT date, intimacy, energy, captured_at FROM persona_snapshots'''
    )
    db.execute('DROP TABLE persona_snapshots')
    db.execute('ALTER TABLE persona_snapshots_v7 RENAME TO persona_snapshots')
    db.execute(
        '''CREATE INDEX idx_persona_snapshots_time
           ON persona_snapshots(captured_at DESC)'''
    )
    after = int(db.execute('SELECT COUNT(*) FROM persona_snapshots').fetchone()[0])
    return before, after


def _assert_migration_integrity(
    db: sqlite3.Connection,
    bond_counts: tuple[int, int],
    snapshot_counts: tuple[int, int],
) -> None:
    """检查 v6 到 v7 迁移没有丢行且保持数据库完整性。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :param bond_counts: 人格关系表迁移前后的行数。
    :param snapshot_counts: 人格快照表迁移前后的行数。
    :return: 所有检查通过时返回 `None`。
    :raises RuntimeError: 行数变化、关键列为空、外键检查或 SQLite 完整性检查失败。
    :side_effects: 只读迁移后的表和 SQLite 检查结果。
    """
    # 两张表都必须保持行数不变，关系维度收敛不能改变历史记录数量。
    if bond_counts[0] != bond_counts[1]:
        raise RuntimeError('v7 迁移自检失败：persona_bond 行数发生变化')
    if snapshot_counts[0] != snapshot_counts[1]:
        raise RuntimeError('v7 迁移自检失败：persona_snapshots 行数发生变化')

    # 关键状态列禁止空值，否则人格描述和睡眠计算无法确定语义。
    for table, column in (
        ('persona_bond', 'intimacy'),
        ('persona_bond', 'updated_at'),
        ('persona_snapshots', 'intimacy'),
        ('persona_snapshots', 'energy'),
        ('persona_snapshots', 'captured_at'),
    ):
        missing = db.execute(
            f'SELECT 1 FROM {table} WHERE {column} IS NULL LIMIT 1'
        ).fetchone()
        if missing is not None:
            raise RuntimeError(f'v7 迁移自检失败：{table}.{column} 存在空值')

    # 外键检查和 SQLite 完整性检查共同覆盖引用关系与底层页结构。
    foreign_key_rows = db.execute('PRAGMA foreign_key_check').fetchall()
    if foreign_key_rows:
        raise RuntimeError('v7 迁移自检失败：外键完整性检查失败')

    integrity_rows = db.execute('PRAGMA integrity_check').fetchall()
    if [row[0] for row in integrity_rows] != ['ok']:
        raise RuntimeError('v7 迁移自检失败：数据库完整性检查失败')


@register(6)
def v6_to_v7(db: sqlite3.Connection) -> None:
    """将关系和人格快照表收敛为当前字段，并保持保留数据的行数与数值不变。

    Args:
        db: 当前迁移事务使用的 SQLite 连接。

    Raises:
        RuntimeError: v6 表结构不符合预期，或迁移后行数、字段、外键和完整性检查失败。
        sqlite3.Error: 表创建、数据复制、表替换或索引创建失败。

    Side Effects:
        替换 ``persona_bond`` 和 ``persona_snapshots`` 表，移除已废弃字段并保留
        intimacy、energy 和时间字段；不提交事务。
    """
    bond_counts = _rebuild_persona_bond(db)
    snapshot_counts = _rebuild_persona_snapshots(db)
    _assert_migration_integrity(db, bond_counts, snapshot_counts)
    logger.info(
        'v6_to_v7_done',
        bonds=bond_counts[1],
        snapshots=snapshot_counts[1],
    )
