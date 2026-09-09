"""「状态」类别退役迁移（v31 -> v32）回归。

钉六件事：存量「状态」全部改判「事件」、衰减派生值按新半衰期重算且被旧曲线
冻住的行该复活就复活、人工置顶行只改类别不动半衰期、重放幂等、缺 facts 表的
最小库直接跳过、停在 v31 的库走完整条链后到达链条头。
"""

from __future__ import annotations

import sqlite3

from src.core.db.migrations.bootstrap import write_user_version
from src.core.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    run_migrations,
)
from src.core.db.migrations.v31_to_v32 import FROM_VERSION, migrate
from src.core.db.schema import DDL, SEED
from src.core.memory.decay import (
    PIN_HALF_LIFE_HOURS,
    REVIVE,
    freeze_due_at,
    half_life_for,
    retention,
)
from src.core.runtime.clock import now as current_time

HOUR = 3_600_000
_EVENT_HALF_LIFE = half_life_for('事件')
# 退役前「状态」的半衰期；构造存量行时直接写字面量，不引用任何现存常量。
_LEGACY_STATUS_HALF_LIFE = 12.0


def _insert_fact(
    db: sqlite3.Connection,
    *,
    kind: str,
    content_key: str,
    strength: float = 1.0,
    half_life: float = _LEGACY_STATUS_HALF_LIFE,
    age_hours: float = 0.0,
    active: int = 1,
) -> int:
    """按给定衰减参数直接落一条事实行，模拟迁移前的存量世界。"""

    now = current_time()
    updated = now - int(age_hours * HOUR)
    cursor = db.execute(
        '''INSERT INTO facts (person_id, kind, content, content_key, strength,
                              half_life_hours, updated_at, created_at, due_at, active)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            kind, f'事实{content_key}', content_key, strength, half_life,
            updated, updated, freeze_due_at(strength, updated, half_life), active,
        ),
    )
    return int(cursor.lastrowid)


def _v31_shaped_db() -> sqlite3.Connection:
    """构造一个 v31 形态的库：结构随当前 DDL，版本号停在 v31。

    v31 -> v32 只改数据不改结构，存量与全新库的差异全在 facts 行的类别与
    衰减派生值里，由各个用例自行写入。
    """

    db = sqlite3.connect(':memory:')
    db.executescript(DDL)
    db.executescript(SEED)
    write_user_version(db, 31)
    return db


def _rows(db: sqlite3.Connection) -> list[tuple]:
    return db.execute(
        'SELECT id, kind, half_life_hours, strength, updated_at, due_at, active'
        ' FROM facts ORDER BY id'
    ).fetchall()


def test_v32_rejudges_status_facts_and_revives_what_the_old_curve_froze() -> None:
    """「状态」行改判「事件」；被 12 小时曲线冻住的行按 504 小时曲线复活。"""

    db = _v31_shaped_db()
    assert FROM_VERSION == 31
    # 十天前写入：旧曲线下早已冻结（active=0），新曲线下留存 0.72，必须复活。
    frozen = _insert_fact(
        db, kind='状态', content_key='frozen', age_hours=240.0, active=0,
    )
    # 边缘行：新留存 0.144 低于 REVIVE，即使原来活跃也应置为不活跃。
    weak = _insert_fact(
        db, kind='状态', content_key='weak', strength=0.2, age_hours=240.0, active=1,
    )
    now = current_time()

    migrate(db)

    rows = {row[0]: row for row in _rows(db)}
    for fact_id in (frozen, weak):
        _, kind, half_life, strength, updated_at, due_at, _ = rows[fact_id]
        assert kind == '事件'
        assert half_life == _EVENT_HALF_LIFE
        assert due_at == freeze_due_at(strength, updated_at, _EVENT_HALF_LIFE)
    assert rows[frozen][6] == 1
    assert rows[weak][6] == 0
    expected_weak = retention(0.2, rows[weak][4], _EVENT_HALF_LIFE, now)
    assert expected_weak < REVIVE
    db.close()


def test_v32_pinned_fact_only_changes_kind() -> None:
    """★K1-4：人工置顶的行半衰期是编码不是类别属性，迁移不得覆盖。"""

    db = _v31_shaped_db()
    pinned = _insert_fact(
        db, kind='状态', content_key='pinned', half_life=PIN_HALF_LIFE_HOURS,
    )
    before = db.execute(
        'SELECT half_life_hours, strength, updated_at, due_at, active FROM facts WHERE id = ?',
        (pinned,),
    ).fetchone()

    migrate(db)

    row = db.execute(
        'SELECT kind, half_life_hours, strength, updated_at, due_at, active FROM facts WHERE id = ?',
        (pinned,),
    ).fetchone()
    assert row[0] == '事件'
    assert row[1:] == before
    db.close()


def test_v32_replay_is_idempotent() -> None:
    """★K1-3：重放命中 0 行，全表逐字节不变。"""

    db = _v31_shaped_db()
    _insert_fact(db, kind='状态', content_key='a', age_hours=240.0, active=0)
    _insert_fact(db, kind='偏好', content_key='b', half_life=half_life_for('偏好'))
    migrate(db)
    before = _rows(db)

    migrate(db)

    assert _rows(db) == before
    db.close()


def test_v32_leaves_no_status_facts_and_half_lives_match_kinds() -> None:
    """★K1-1 / ★K1-2：迁移后没有「状态」行，全表半衰期与类别逐行一致。"""

    db = _v31_shaped_db()
    _insert_fact(db, kind='状态', content_key='s1')
    _insert_fact(db, kind='状态', content_key='s2', half_life=PIN_HALF_LIFE_HOURS)
    for kind in ('身份', '日期', '偏好', '习惯', '关系', '事件'):
        _insert_fact(db, kind=kind, content_key=f'k-{kind}', half_life=half_life_for(kind))

    migrate(db)

    assert db.execute("SELECT COUNT(*) FROM facts WHERE kind = '状态'").fetchone()[0] == 0
    rows = db.execute('SELECT id, kind, half_life_hours FROM facts').fetchall()
    assert rows, '本用例必须真的构造了事实行'
    for fact_id, kind, half_life in rows:
        if half_life >= PIN_HALF_LIFE_HOURS:
            continue
        assert half_life == half_life_for(kind), (
            f'facts.id={fact_id} 半衰期 {half_life!r} 与类别 {kind!r} 不一致'
        )
    db.close()


def test_v32_skips_database_without_facts_table() -> None:
    """部分早期最小库没有 facts 表，链尾 DDL 会建表；迁移自身不建。"""

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE probe (id INTEGER PRIMARY KEY)')

    migrate(db)

    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'facts'"
    ).fetchone() is None
    db.close()


def test_chain_from_v31_reaches_chain_head() -> None:
    """停在 v31 的存量库走完整条链后到达链条头，且不再有任何「状态」行。"""

    db = _v31_shaped_db()
    _insert_fact(db, kind='状态', content_key='chained', age_hours=240.0, active=0)

    run_migrations(db)

    assert get_user_version(db) == CURRENT_VERSION
    assert db.execute("SELECT COUNT(*) FROM facts WHERE kind = '状态'").fetchone()[0] == 0
    db.close()


def test_chain_from_v18_carries_retired_kind_to_event() -> None:
    """≤v18 的存量库先经 v19 映射到「状态」，再经本迁移归「事件」，链条不断。

    v19 的映射表是历史事实，不能把旧类别直接写成「事件」；本用例钉住的是
    链条接力之后的终态。
    """

    db = sqlite3.connect(':memory:')
    db.executescript(DDL)
    db.executescript(SEED)
    now = current_time()
    db.execute(
        '''INSERT INTO facts (person_id, kind, content, content_key, strength,
                              half_life_hours, updated_at, created_at, due_at, active)
           VALUES (1, '开发进度', '存量旧类别事实', 'legacy-kind', 0.8, 720.0, ?, ?, ?, 1)''',
        (now, now, freeze_due_at(0.8, now, 720.0)),
    )
    write_user_version(db, 18)

    run_migrations(db)

    assert get_user_version(db) == CURRENT_VERSION
    row = db.execute(
        "SELECT kind, half_life_hours FROM facts WHERE content_key = 'legacy-kind'"
    ).fetchone()
    assert row == ('事件', half_life_for('事件'))
    db.close()
