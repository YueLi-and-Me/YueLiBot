"""去重键重算迁移（v33 -> v34）回归。

钉六件事：数字之间带标点的存量行改写为新键、其余行不动、键与旧口径不符的行不在
范围内、重放幂等、缺表的最小库直接跳过、停在 v33 的库走完链条后同一正文重写命中
旧行而不是另起一行。
"""

from __future__ import annotations

import re
import sqlite3

from src.core.db.migrations.bootstrap import write_user_version
from src.core.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    run_migrations,
)
from src.core.db.migrations.v33_to_v34 import FROM_VERSION, migrate
from src.core.db.schema import DDL, SEED
from src.core.memory.similarity import exact_key
from src.core.memory.store import FactInput, MemoryStore

NOW = 1_800_000_000_000
VERSION_FACT = '凌白发布了yuelibot的0.1.5版本'
PLAIN_FACT = '凌白喜欢喝冰美式'
VERSION_NOTE = '0.1.5 版本起事实去重键保留数字之间的分隔'


def _legacy_key(text: str) -> str:
    """迁移前的去重键口径：删除全部标点与符号。构造存量行时不引用现行实现。"""

    return re.sub(r'[^\w]', '', text).lower()


def _v33_shaped_db() -> sqlite3.Connection:
    """构造 v33 形态的库：结构随当前 DDL，版本号停在 v33，键按旧口径写入。"""

    db = sqlite3.connect(':memory:')
    db.executescript(DDL)
    db.executescript(SEED)
    write_user_version(db, 33)
    for content in (VERSION_FACT, PLAIN_FACT):
        db.execute(
            '''INSERT INTO facts (person_id, kind, content, content_key, strength,
                                  half_life_hours, updated_at, created_at, due_at, active)
               VALUES (1, '事件', ?, ?, 1.0, 504.0, ?, ?, ?, 1)''',
            (content, _legacy_key(content), NOW, NOW, NOW),
        )
    db.execute(
        'INSERT INTO knowledge (content, content_key, source, created_at) VALUES (?, ?, ?, ?)',
        (VERSION_NOTE, _legacy_key(VERSION_NOTE), 'test', NOW),
    )
    db.commit()
    return db


def _keys(db: sqlite3.Connection, table: str) -> dict[str, str]:
    return dict(db.execute(f'SELECT content, content_key FROM {table}').fetchall())


def test_v34_rekeys_rows_with_separated_digits_only() -> None:
    db = _v33_shaped_db()
    assert FROM_VERSION == 33
    assert _keys(db, 'facts')[VERSION_FACT] == _legacy_key(VERSION_FACT) != exact_key(VERSION_FACT)

    migrate(db)

    assert _keys(db, 'facts') == {
        VERSION_FACT: exact_key(VERSION_FACT),
        PLAIN_FACT: _legacy_key(PLAIN_FACT),
    }
    assert _keys(db, 'knowledge') == {VERSION_NOTE: exact_key(VERSION_NOTE)}
    db.close()


def test_v34_leaves_rows_not_written_by_legacy_rule_untouched() -> None:
    """键与旧口径本就不符的行来自更早的写入路径，不在本次重算范围内。"""

    db = _v33_shaped_db()
    db.execute(
        '''INSERT INTO facts (person_id, kind, content, content_key, strength,
                              half_life_hours, updated_at, created_at, due_at, active)
           VALUES (1, '事件', '他在2024.10.12搬家', 'stale-key', 1.0, 504.0, ?, ?, ?, 1)''',
        (NOW, NOW, NOW),
    )

    migrate(db)

    assert _keys(db, 'facts')['他在2024.10.12搬家'] == 'stale-key'
    db.close()


def test_v34_replay_is_idempotent() -> None:
    db = _v33_shaped_db()
    migrate(db)
    before = (_keys(db, 'facts'), _keys(db, 'knowledge'))

    migrate(db)

    assert (_keys(db, 'facts'), _keys(db, 'knowledge')) == before
    db.close()


def test_v34_skips_database_without_tables() -> None:
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE probe (id INTEGER PRIMARY KEY)')

    migrate(db)

    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('facts', 'knowledge')"
    ).fetchone()[0] == 0
    db.close()


def test_chain_from_v33_lets_same_content_hit_the_existing_row() -> None:
    """迁移后同一正文重写走精确键命中旧行；不重算时会算出新键另起一行。"""

    db = _v33_shaped_db()

    run_migrations(db)

    assert get_user_version(db) == CURRENT_VERSION
    old_id = db.execute('SELECT id FROM facts WHERE content = ?', (VERSION_FACT,)).fetchone()[0]
    written = MemoryStore(db).add_fact(1, FactInput(content=VERSION_FACT, kind='事件'), NOW + 1)
    assert written.fact_id == old_id and written.created is False
    assert db.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 2
    db.close()
