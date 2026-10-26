"""验证动态回合扣费的量级、跨会话共享、持久化与事务边界。"""

from datetime import datetime
from pathlib import Path
from typing import List, Tuple

import json
import pytest
import sqlite3

from src.core.persona.state import EventDelta, Persona
from src.core.persona.turn_energy import TURN_ENERGY_LOAD_KEY, TurnEnergyLoad
from src.core.platform_io.registry import StreamRegistry


HOUR = 3_600_000
DAY = 24 * HOUR
START = 1_800_000_000_000


def _chat_day(load: TurnEnergyLoad, day: int, turns: int) -> Tuple[float, TurnEnergyLoad]:
    """每天清醒 16 小时内均匀聊天，累计理论扣费，不混入精力触底截断。"""
    total = 0.0
    for turn in range(turns):
        now = day * DAY + 8 * HOUR + (turn + 1) * 16 * HOUR // turns
        cost, load = load.charge(now, 0.8)
        assert cost > 0
        total += cost
    return total, load


@pytest.mark.parametrize(
    ('turns', 'lower', 'upper'),
    [(20, 3.5, 4.5), (100, 11.0, 13.0), (200, 15.0, 17.0), (500, 19.0, 21.0), (1000, 21.0, 23.0)],
)
def test_different_daily_volumes_keep_meaningful_bounded_cost(
    turns: int, lower: float, upper: float,
) -> None:
    load = TurnEnergyLoad(0.0, 0)
    totals: List[float] = []
    for day in range(21):
        cost, load = _chat_day(load, day, turns)
        totals.append(cost)

    # 独立预算区间同时挡住按固定费率无限放大，以及高频时几乎免费两种退化。
    assert lower < totals[-1] < upper
    assert totals[-1] == pytest.approx(totals[-2], rel=1e-5)
    assert totals[0] > totals[-1]


def test_unusual_busy_day_still_costs_more_than_normal_day() -> None:
    load = TurnEnergyLoad(0.0, 0)
    for day in range(14):
        _, load = _chat_day(load, day, 100)
    normal_cost, _ = _chat_day(load, 14, 100)
    busy_cost, _ = _chat_day(load, 14, 300)

    assert normal_cost < busy_cost < normal_cost * 3


def test_midnight_does_not_reset_load_and_rest_restores_coefficient() -> None:
    before_midnight = int(datetime(2051, 7, 15, 23, 59).timestamp() * 1000)
    load = TurnEnergyLoad(80.0, before_midnight)
    initial_cost, _ = load.charge(before_midnight, 0.8)
    midnight_cost, _ = load.charge(before_midnight + 2 * 60_000, 0.8)
    rested_cost, _ = load.charge(before_midnight + DAY, 0.8)

    assert initial_cost == pytest.approx(0.12)
    assert midnight_cost == pytest.approx(initial_cost, rel=0.002)
    assert midnight_cost < rested_cost < 0.24


def test_load_survives_database_reopen(db: sqlite3.Connection, tmp_path: Path) -> None:
    persona = Persona(db)
    for index in range(50):
        persona.apply_turn(1, START + index * 60_000, weight=0.8)
    path = tmp_path / 'persona.db'
    with sqlite3.connect(path) as saved:
        db.backup(saved)

    expected = persona.apply_turn(1, START + HOUR, weight=0.8)
    with sqlite3.connect(path) as reopened:
        reopened.row_factory = sqlite3.Row
        actual = Persona(reopened).apply_turn(1, START + HOUR, weight=0.8)
        assert actual == expected


def test_all_people_share_load_but_keep_bonds_separate(db: sqlite3.Connection) -> None:
    registry = StreamRegistry(db)
    contact = registry.create_person('contact', first_seen_at=START)
    persona = Persona(db)
    owner_before = persona.get(1)
    contact_before = persona.get(contact.id)
    settled_at = persona.settled_at()

    owner_after = persona.apply_turn(1, START, weight=0.8)
    contact_after = persona.apply_turn(contact.id, START, weight=0.8)

    first_cost = owner_before.energy - owner_after.energy
    second_cost = owner_after.energy - contact_after.energy
    assert 0 < second_cost < first_cost
    assert owner_after.intimacy - owner_before.intimacy == pytest.approx(0.28)
    assert contact_after.intimacy - contact_before.intimacy == pytest.approx(0.28)
    assert persona.get(1).intimacy == owner_after.intimacy
    assert contact_after.mood == owner_before.mood
    assert persona.settled_at() == settled_at


def test_zero_weight_and_model_events_do_not_add_load(db: sqlite3.Connection) -> None:
    persona = Persona(db)
    before = persona.get(1)
    zero = persona.apply_turn(1, START, weight=0.0)
    assert zero.energy == before.energy
    assert zero.intimacy == before.intimacy
    load_before = db.execute('SELECT value FROM meta WHERE key=?', (TURN_ENERGY_LOAD_KEY,)).fetchone()[0]
    event = persona.apply_event(1, EventDelta(energy=-1), START, weight=0.8)
    assert zero.energy - event.energy == pytest.approx(0.8)
    assert db.execute('SELECT value FROM meta WHERE key=?', (TURN_ENERGY_LOAD_KEY,)).fetchone()[0] == load_before
    first = persona.apply_turn(1, START, weight=0.8)
    assert event.energy - first.energy == pytest.approx(0.24)


def test_disabled_energy_still_learns_load_without_charging(db: sqlite3.Connection) -> None:
    persona = Persona(db)
    before = persona.get(1)
    persona.set_energy_enabled(False, START)
    for _ in range(100):
        persona.apply_turn(1, START, weight=0.8)
    assert persona.get(1).energy == before.energy

    persona.set_energy_enabled(True, START)
    after = persona.apply_turn(1, START, weight=0.8)
    assert before.energy - after.energy == pytest.approx(0.12)


def test_load_and_persona_updates_rollback_together(db: sqlite3.Connection) -> None:
    persona = Persona(db)
    before = persona.apply_turn(1, START, weight=0.8)
    load_before = db.execute('SELECT value FROM meta WHERE key=?', (TURN_ENERGY_LOAD_KEY,)).fetchone()[0]
    db.execute(
        "CREATE TRIGGER reject_turn_load BEFORE UPDATE ON meta "
        "WHEN NEW.key = 'persona_turn_energy_load' "
        "BEGIN SELECT RAISE(ABORT, '拒绝负载写入'); END"
    )
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match='拒绝负载写入'):
        persona.apply_turn(1, START + 60_000, weight=0.8)

    assert persona.get(1) == before
    assert db.execute('SELECT value FROM meta WHERE key=?', (TURN_ENERGY_LOAD_KEY,)).fetchone()[0] == load_before


def test_corrupt_load_is_reported_without_resetting_it(db: sqlite3.Connection) -> None:
    persona = Persona(db)
    before = persona.get(1)
    corrupt = json.dumps({'effective_turns': -1, 'updated_at': START})
    db.execute('INSERT INTO meta (key,value) VALUES (?,?)', (TURN_ENERGY_LOAD_KEY, corrupt))
    db.commit()

    with pytest.raises(ValueError, match='非负有限数'):
        persona.apply_turn(1, START, weight=0.8)
    assert persona.get(1) == before
    assert db.execute('SELECT value FROM meta WHERE key=?', (TURN_ENERGY_LOAD_KEY,)).fetchone()[0] == corrupt


def test_clock_reversal_is_reported() -> None:
    with pytest.raises(ValueError, match='不能早于'):
        TurnEnergyLoad(1.0, START).charge(START - 1, 0.8)


@pytest.mark.parametrize('weight', [-1.0, 1.1, float('nan'), float('inf')])
def test_invalid_weight_is_reported(weight: float) -> None:
    with pytest.raises(ValueError, match='0 到 1'):
        TurnEnergyLoad(0.0, START).charge(START, weight)
