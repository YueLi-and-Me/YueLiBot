"""P0 事实类别迁移与衰减派生值回归。"""

from __future__ import annotations

import sqlite3

from src.core.db.migrations.v18_to_v19 import KIND_MAPPING, migrate
from src.core.memory.decay import FACT_KINDS, freeze_due_at, half_life_for
from src.core.memory.store import MemoryStore

NOW = 1_800_000_000_000


def test_kind_migration_maps_literal_table_and_recomputes_decay() -> None:
    """★C-1/★C-2：类别、半衰期与 due_at 同步迁移且重放幂等。"""

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    MemoryStore(db)
    source_kinds = list(KIND_MAPPING) + ['任务书未列出的旧类别']
    original = {}
    for index, source_kind in enumerate(source_kinds):
        strength = 0.55 + index / 1000
        updated_at = NOW + index * 100
        cursor = db.execute(
            '''INSERT INTO facts (
                   person_id, kind, content, content_key, strength, half_life_hours,
                   updated_at, created_at, due_at, active
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)''',
            (
                1,
                source_kind,
                f'迁移存量行 {index}',
                f'migration-row-{index}',
                strength,
                720.0,
                updated_at,
                updated_at,
                1,
            ),
        )
        fact_id = int(cursor.lastrowid)
        original[fact_id] = (strength, updated_at, source_kind)
    db.commit()

    migrate(db)

    rows = db.execute(
        '''SELECT id, kind, strength, half_life_hours, updated_at, due_at
           FROM facts ORDER BY id'''
    ).fetchall()
    assert {row['kind'] for row in rows} <= FACT_KINDS
    for row in rows:
        strength, updated_at, source_kind = original[row['id']]
        expected_kind = KIND_MAPPING.get(source_kind, '事件')
        expected_half_life = half_life_for(expected_kind)
        assert row['kind'] == expected_kind
        assert row['strength'] == strength
        assert row['updated_at'] == updated_at
        assert row['half_life_hours'] == expected_half_life
        assert row['due_at'] == freeze_due_at(strength, updated_at, expected_half_life)

    before_replay = [tuple(row) for row in rows]
    migrate(db)
    after_replay = [tuple(row) for row in db.execute(
        '''SELECT id, kind, strength, half_life_hours, updated_at, due_at
           FROM facts ORDER BY id'''
    )]
    assert after_replay == before_replay
    db.close()


def test_kind_migration_accepts_early_database_without_facts() -> None:
    """早期最小库没有事实表时无存量可重算，迁移应保持空操作。"""

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE legacy_probe (id INTEGER PRIMARY KEY)')

    migrate(db)

    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'facts'"
    ).fetchone()[0] == 0
    db.close()
