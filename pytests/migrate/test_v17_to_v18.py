"""v18 → v18 迁移验收：三列补齐、幂等重放、既有数据无损。"""

from __future__ import annotations

import sqlite3

from src.core.common.db.migrations.v17_to_v18 import migrate


def _columns(db: sqlite3.Connection) -> list[str]:
    return [str(row[1]) for row in db.execute("SELECT * FROM pragma_table_info('jargon')")]


def _build_v18_jargon(db: sqlite3.Connection, rows: int) -> None:
    """建一个 v18 形态的 jargon 表（没有学习三列）并塞入存量行。"""

    db.executescript(
        '''
        CREATE TABLE jargon (
          id         INTEGER PRIMARY KEY,
          term       TEXT    NOT NULL,
          meaning    TEXT    NOT NULL,
          stream_id  INTEGER,
          status     TEXT    NOT NULL DEFAULT 'confirmed',
          hits       INTEGER NOT NULL DEFAULT 0,
          source     TEXT    NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL,
          UNIQUE(term, stream_id)
        );
        '''
    )
    db.executemany(
        "INSERT INTO jargon (term, meaning, stream_id, status, hits, source, created_at)"
        " VALUES (?, ?, NULL, 'confirmed', 0, '历史迁移:M-3', 1756000000000)",
        [(f'词{index}', f'释义{index}') for index in range(rows)],
    )
    db.commit()


def test_v18_adds_three_columns_and_keeps_rows() -> None:
    db = sqlite3.connect(':memory:')
    _build_v18_jargon(db, rows=1000)
    migrate(db)

    columns = _columns(db)
    for name in ('sightings', 'evidence_ids', 'inferred_at_sightings'):
        assert name in columns
    row = db.execute(
        'SELECT COUNT(*), MIN(sightings), MAX(sightings),'
        ' MAX(inferred_at_sightings) FROM jargon',
    ).fetchone()
    assert row[0] == 1000
    assert row[1] == 0 and row[2] == 0 and row[3] == 0
    db.close()


def test_v18_replay_is_idempotent_and_keeps_learned_values() -> None:
    """重放跳过已存在列；已累积的 sightings 与推断进度不被清掉。"""

    db = sqlite3.connect(':memory:')
    _build_v18_jargon(db, rows=3)
    migrate(db)
    db.execute(
        "UPDATE jargon SET sightings = 9, inferred_at_sightings = 8,"
        " evidence_ids = '[1,2,3]' WHERE id = 1")
    db.commit()

    migrate(db)  # 重放：不执行任何 DDL，也不触发自检

    row = db.execute(
        'SELECT sightings, inferred_at_sightings, evidence_ids FROM jargon WHERE id = 1',
    ).fetchone()
    assert row == (9, 8, '[1,2,3]')
    db.close()


def test_v18_rejects_wrong_column_type() -> None:
    """列存在但类型不对（健康库不应出现）时迁移必须喊出来。"""

    db = sqlite3.connect(':memory:')
    _build_v18_jargon(db, rows=1)
    db.execute('ALTER TABLE jargon ADD COLUMN sightings TEXT')
    db.commit()
    try:
        migrate(db)
    except RuntimeError as exc:
        assert 'sightings' in str(exc)
    else:
        raise AssertionError('类型不对的列没有被迁移自检拦下')
    db.close()
