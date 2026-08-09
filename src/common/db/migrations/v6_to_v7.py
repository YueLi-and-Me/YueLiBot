"""v6 → v7 迁移：关系状态收敛为按人物保存的单一好感度。"""

from __future__ import annotations

import sqlite3

from .registry import register

from src.common.logger import get_logger

logger = get_logger(__name__)


def _table_columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in db.execute(f'PRAGMA table_info({table})').fetchall()]


def _rebuild_persona_bond(db: sqlite3.Connection) -> tuple[int, int]:
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
    columns = _table_columns(db, 'persona_snapshots')
    expected = ['date', 'intimacy', 'tsundere', 'reliance', 'energy', 'captured_at']
    if columns != expected:
        raise RuntimeError(
            f'v6 数据库的 persona_snapshots 结构不符合预期：{columns}'
        )

    before = int(db.execute('SELECT COUNT(*) FROM persona_snapshots').fetchone()[0])
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
    if bond_counts[0] != bond_counts[1]:
        raise RuntimeError('v7 迁移自检失败：persona_bond 行数发生变化')
    if snapshot_counts[0] != snapshot_counts[1]:
        raise RuntimeError('v7 迁移自检失败：persona_snapshots 行数发生变化')

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

    foreign_key_rows = db.execute('PRAGMA foreign_key_check').fetchall()
    if foreign_key_rows:
        raise RuntimeError('v7 迁移自检失败：外键完整性检查失败')

    integrity_rows = db.execute('PRAGMA integrity_check').fetchall()
    if [row[0] for row in integrity_rows] != ['ok']:
        raise RuntimeError('v7 迁移自检失败：数据库完整性检查失败')


@register(6)
def v6_to_v7(db: sqlite3.Connection) -> None:
    """丢弃两个人设专属维度，并保持好感度原值不变。"""
    bond_counts = _rebuild_persona_bond(db)
    snapshot_counts = _rebuild_persona_snapshots(db)
    _assert_migration_integrity(db, bond_counts, snapshot_counts)
    logger.info(
        'v6_to_v7_done',
        bonds=bond_counts[1],
        snapshots=snapshot_counts[1],
    )
