"""验证好感度单轴模型和数据库迁移后的数值保持。

本模块覆盖关系计算、旧人格字段迁移和迁移前备份，确保旧数据中的亲密度能够安全转换，
并且废弃字段不会继续影响当前运行时。
"""

from __future__ import annotations

from pathlib import Path

import sqlite3

import pytest

from src.core.awareness.interest import factors_for
from src.core.common.db.migrations.manager import CURRENT_VERSION, run_migrations
from src.core.persona.state import PersonaState, describe_persona


NOW = 1_700_000_100_000


@pytest.mark.parametrize('favor', [5.0, 30.0, 55.0, 75.0, 95.0])
def test_describe_persona_has_five_neutral_favor_bands(favor: float) -> None:
    """五档只描述关系深浅，不替可配置人设决定表达风格。"""
    description = describe_persona(
        PersonaState(intimacy=favor, energy=60.0, mood=50.0, updated_at=NOW),
    )

    for forbidden in ('撒娇', '吃醋', '嘴硬', '别扭', '黏'):
        assert forbidden not in description


def test_all_five_favor_bands_are_distinct() -> None:
    descriptions = {
        describe_persona(
            PersonaState(
                intimacy=favor,
                energy=60.0,
                mood=50.0,
                updated_at=NOW,
            )
        )
        for favor in (5.0, 30.0, 55.0, 75.0, 95.0)
    }

    assert len(descriptions) == 5


def test_interest_uses_favor_without_changing_rate_scale() -> None:
    """旧 reliance=50 的 2.4/min 基准迁移到 favor=50 后保持同量级。"""
    factors = factors_for(
        activity='idle',
        intensity='light',
        favor=50.0,
        energy=50.0,
        ignored=0,
        absence_hours=4.0,
    )

    assert factors.rate_per_minute == pytest.approx(2.4)
    assert factors.as_trace()['fFavor'] == 0.5
    assert 'fReliance' not in factors.as_trace()


def _build_v6_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE messages (
          id INTEGER PRIMARY KEY,
          role TEXT NOT NULL,
          content TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          episode_id INTEGER,
          stream_id INTEGER NOT NULL DEFAULT 1,
          sender_person_id INTEGER
        );

        CREATE TABLE persons (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL,
          first_seen_at INTEGER NOT NULL
        );

        -- streams 由 v5→v6 迁移创建，真实的 v6 库必然带有该表；夹具漏建会让后续
        -- 依赖会话归属的迁移在这里失败，而失败原因与被测的人格轴迁移无关。
        CREATE TABLE streams (
          id INTEGER PRIMARY KEY,
          platform TEXT NOT NULL,
          kind TEXT NOT NULL,
          external_id TEXT NOT NULL,
          UNIQUE(platform, kind, external_id)
        );

        CREATE TABLE persona_bond (
          person_id INTEGER PRIMARY KEY REFERENCES persons(id),
          intimacy REAL NOT NULL,
          tsundere REAL NOT NULL,
          reliance REAL NOT NULL,
          updated_at INTEGER NOT NULL
        );

        CREATE TABLE persona_self (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          energy REAL NOT NULL,
          updated_at INTEGER NOT NULL
        );

        CREATE TABLE persona_snapshots (
          date TEXT PRIMARY KEY,
          intimacy REAL NOT NULL,
          tsundere REAL NOT NULL,
          reliance REAL NOT NULL,
          energy REAL NOT NULL,
          captured_at INTEGER NOT NULL
        );
        """
    )
    db.executemany(
        'INSERT INTO persons (id, kind, first_seen_at) VALUES (?, ?, ?)',
        [
            (1, 'owner', NOW - 86_400_000),
            (2, 'contact', NOW),
        ],
    )
    db.executemany(
        '''INSERT INTO persona_bond (
               person_id, intimacy, tsundere, reliance, updated_at
           ) VALUES (?, ?, ?, ?, ?)''',
        [
            (1, 67.0, -8.0, 41.0, NOW),
            (2, 23.0, 11.0, 17.0, NOW + 1),
        ],
    )
    db.execute(
        'INSERT INTO persona_self (id, energy, updated_at) VALUES (1, 73.0, ?)',
        (NOW,),
    )
    db.execute(
        '''INSERT INTO persona_snapshots (
               date, intimacy, tsundere, reliance, energy, captured_at
           ) VALUES (?, ?, ?, ?, ?, ?)''',
        ('2026-08-08', 66.0, -7.0, 40.0, 74.0, NOW - 86_400_000),
    )
    db.execute('PRAGMA user_version = 6')
    db.commit()
    db.close()


def test_v6_to_v7_keeps_favor_and_snapshots_with_backup(tmp_path: Path) -> None:
    """旧库先备份再迁移；旧亲密度原值转换为好感度，废弃字段不再写入当前模型。"""
    path = tmp_path / 'memory.db'
    _build_v6_database(path)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row

    run_migrations(db, path)

    assert db.execute('PRAGMA user_version').fetchone()[0] == CURRENT_VERSION
    assert [row[1] for row in db.execute('PRAGMA table_info(persona_bond)')] == [
        'person_id',
        'intimacy',
        'updated_at',
    ]
    assert [tuple(row) for row in db.execute(
        'SELECT person_id, intimacy, updated_at FROM persona_bond ORDER BY person_id'
    )] == [
        (1, 67.0, NOW),
        (2, 23.0, NOW + 1),
    ]
    assert [row[1] for row in db.execute('PRAGMA table_info(persona_snapshots)')] == [
        'date',
        'intimacy',
        'energy',
        'captured_at',
        'mood',
    ]
    assert tuple(db.execute(
        'SELECT date, intimacy, energy, mood, captured_at FROM persona_snapshots'
    ).fetchone()) == ('2026-08-08', 66.0, 74.0, 50.0, NOW - 86_400_000)

    before = {
        'bonds': db.execute('SELECT COUNT(*) FROM persona_bond').fetchone()[0],
        'snapshots': db.execute('SELECT COUNT(*) FROM persona_snapshots').fetchone()[0],
    }
    run_migrations(db, path)
    after = {
        'bonds': db.execute('SELECT COUNT(*) FROM persona_bond').fetchone()[0],
        'snapshots': db.execute('SELECT COUNT(*) FROM persona_snapshots').fetchone()[0],
    }
    assert after == before
    db.close()

    backups = list((tmp_path / 'backups').glob('memory.v6.*.db'))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    assert backup.execute('PRAGMA user_version').fetchone()[0] == 6
    assert tuple(backup.execute(
        'SELECT intimacy, tsundere, reliance FROM persona_bond WHERE person_id = 1'
    ).fetchone()) == (67.0, -8.0, 41.0)
    backup.close()
