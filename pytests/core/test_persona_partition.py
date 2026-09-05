"""验证人物关系状态与全局身体状态的存储分区契约。

本模块覆盖不同人物的关系轴、全局 energy 单例和 MemoryStore 的读取边界，
确保人物画像不会共享不属于当前人物的状态。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.memory.store import MemoryStore
from src.core.persona.state import EventDelta, Persona
from src.core.platform_io.registry import StreamRegistry


HOUR = 3_600_000
DAY = 24 * HOUR


@pytest.fixture
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    MemoryStore(connection)
    yield connection
    connection.close()


def test_contact_bond_is_isolated_while_energy_is_global(db: sqlite3.Connection) -> None:
    registry = StreamRegistry(db)
    owner = registry.owner_person()
    contact = registry.create_person('contact', first_seen_at=1_700_000_000_000)
    persona = Persona(db)

    legacy_before = tuple(db.execute(
        'SELECT intimacy, tsundere, reliance, energy, updated_at FROM persona WHERE id = 1'
    ).fetchone())
    owner_before = persona.get(owner.id)
    contact_before = persona.get(contact.id)
    contact_after = persona.apply_event(
        contact.id,
        EventDelta(favor=3, energy=2),
        owner_before.updated_at + HOUR,
        weight=1.0,
    )
    owner_after = persona.get(owner.id)

    assert contact_after.intimacy > contact_before.intimacy
    assert owner_after.intimacy == owner_before.intimacy
    assert owner_after.energy == contact_after.energy
    assert owner_after.energy > owner_before.energy
    legacy_after = tuple(db.execute(
        'SELECT intimacy, tsundere, reliance, energy, updated_at FROM persona WHERE id = 1'
    ).fetchone())
    assert legacy_after == legacy_before


def test_non_owner_elapsed_and_snapshots_do_not_change_relation(db: sqlite3.Connection) -> None:
    registry = StreamRegistry(db)
    contact = registry.create_person('contact', first_seen_at=1_700_000_000_000)
    persona = Persona(db)

    before = persona.get(contact.id)
    after = persona.apply_elapsed(contact.id, before.updated_at + 7 * DAY)

    assert after == before
    with pytest.raises(ValueError, match='owner'):
        persona.snapshot_daily(contact.id, before.updated_at)


def test_missing_owner_bond_is_data_corruption(db: sqlite3.Connection) -> None:
    db.execute('DELETE FROM persona_bond WHERE person_id = 1')
    db.commit()

    with pytest.raises(RuntimeError, match='owner'):
        Persona(db).get(1)
