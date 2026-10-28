"""表达学习三列与游标回填迁移（v36 -> v37）回归。

钉六件事：三列补齐且存量行取声明默认值、其它列原样保留；学习游标回拨到回填窗口内
第一条消息之前；窗口内没有消息或当前游标已经更早的会话不动；重放幂等、不重复自检；
游标不是 JSON 对象时只告警不抛；停在 v36 的库走完迁移链后到达链条头。
"""

from __future__ import annotations

from typing import Dict, List

import json
import sqlite3

import pytest

from src.core.agent.expression_learn import CURSOR_KEY
from src.core.db.migrations import v36_to_v37
from src.core.db.migrations.bootstrap import write_user_version
from src.core.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    load_migration_registry,
    run_migrations,
)
from src.core.db.migrations.v36_to_v37 import BACKFILL_DAYS, FROM_VERSION

NOW = 1_800_000_000_000
DAY_MS = 24 * 3600 * 1000
WINDOW_MS = BACKFILL_DAYS * DAY_MS

# v36 形态的结构必须手工建表：当前 DDL 已带 v37 的三列，executescript(DDL) 造出来的
# 库不再是 v36 形态，迁移会因列已存在而跳过补列，用例就验不到补列与自检。
_V36_DDL = """
CREATE TABLE streams (
  id           INTEGER PRIMARY KEY,
  platform     TEXT    NOT NULL,
  kind         TEXT    NOT NULL,
  external_id  TEXT    NOT NULL,
  display_name TEXT,
  UNIQUE(platform, kind, external_id)
);
CREATE TABLE messages (
  id                  INTEGER PRIMARY KEY,
  role                TEXT    NOT NULL,
  content             TEXT    NOT NULL,
  created_at          INTEGER NOT NULL,
  episode_id          INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
  stream_id           INTEGER NOT NULL DEFAULT 1,
  sender_person_id    INTEGER,
  external_message_id TEXT
);
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE expressions (
  id           INTEGER PRIMARY KEY,
  situation    TEXT    NOT NULL,
  style        TEXT    NOT NULL,
  stream_id    INTEGER REFERENCES streams(id) ON DELETE CASCADE,
  use_count    INTEGER NOT NULL DEFAULT 0,
  source       TEXT    NOT NULL DEFAULT '',
  created_at   INTEGER NOT NULL,
  checked      INTEGER NOT NULL DEFAULT 0,
  last_used_at INTEGER,
  UNIQUE(situation, style, stream_id)
);
"""


class _LoggerRecorder:
    """按事件名记录日志调用，替代迁移模块的模块级 logger。"""

    def __init__(self) -> None:
        self.events: List[str] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.events.append(event)

    def warning(self, event: str, **kwargs: object) -> None:
        self.events.append(event)


def _v36_shaped_db(cursors: Dict[str, int] | None = None) -> sqlite3.Connection:
    """构造停在 v36 形态的库，并固定消息布局供各用例断言回拨结果。

    消息布局（id 与相对 NOW 的偏移）：

    - stream 3：id 10（20 天前，窗口外）、id 11（13 天前，窗口内最早）、id 12（1 天前）；
    - stream 5：id 20（30 天前，窗口外）、id 21（2 天前，窗口内最早）；
    - stream 7：id 30（40 天前，窗口内没有消息）。

    :param cursors: 写入 meta.expression_learn_cursor 的会话游标；None 表示不写该键，
        用于验证键缺失时不创建记录。
    :return: 已提交的 v36 形态内存库连接。
    """

    db = sqlite3.connect(':memory:')
    db.executescript(_V36_DDL)
    db.executemany(
        "INSERT INTO streams (id, platform, kind, external_id) "
        "VALUES (?, 'qq', 'group', ?)",
        [(3, 'g3'), (5, 'g5'), (7, 'g7')],
    )
    db.executemany(
        "INSERT INTO messages (id, role, content, created_at, stream_id) "
        "VALUES (?, 'user', '消息', ?, ?)",
        [
            (10, NOW - 20 * DAY_MS, 3),
            (11, NOW - 13 * DAY_MS, 3),
            (12, NOW - 1 * DAY_MS, 3),
            (20, NOW - 30 * DAY_MS, 5),
            (21, NOW - 2 * DAY_MS, 5),
            (30, NOW - 40 * DAY_MS, 7),
        ],
    )
    db.executemany(
        "INSERT INTO expressions (id, situation, style, stream_id, use_count, source, "
        "created_at, checked, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, '情境甲', '说法甲', 3, 7, '本机学习', NOW - 5_000, 1, NOW - 1_000),
            (2, '情境乙', '说法乙', 5, 0, '', NOW - 9_000, -1, None),
        ],
    )
    if cursors is not None:
        db.execute(
            'INSERT INTO meta (key, value) VALUES (?, ?)',
            (CURSOR_KEY, json.dumps(cursors, ensure_ascii=False)),
        )
    write_user_version(db, FROM_VERSION)
    db.commit()
    return db


def _cursors(db: sqlite3.Connection) -> Dict[str, int]:
    """读出 meta.expression_learn_cursor 并解码为会话游标字典。"""

    value = db.execute(
        'SELECT value FROM meta WHERE key = ?', (CURSOR_KEY,),
    ).fetchone()[0]
    return json.loads(value)


@pytest.fixture
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """把迁移模块的统一时钟固定在 NOW，回填窗口边界因此确定。"""

    monkeypatch.setattr(v36_to_v37, 'current_time', lambda: NOW)


def test_v37_adds_learning_columns_and_keeps_existing_rows(
    fixed_clock: None,
) -> None:
    """三列补齐：存量行取声明默认值，其它列原样保留，新插入行同样取默认值。"""

    db = _v36_shaped_db({'3': 11})
    assert FROM_VERSION == CURRENT_VERSION - 1

    v36_to_v37.migrate(db)

    columns = {
        str(row[1]): (str(row[2]), int(row[3]), row[4])
        for row in db.execute('PRAGMA table_info(expressions)')
    }
    assert columns['learn_count'] == ('INTEGER', 1, '1')
    assert columns['reading'] == ('TEXT', 1, "''")
    assert columns['quote'] == ('TEXT', 1, "''")
    # 迁移不得改写任何存量行的既有列。
    assert db.execute(
        'SELECT id, situation, style, stream_id, use_count, source, created_at, '
        'checked, last_used_at FROM expressions ORDER BY id'
    ).fetchall() == [
        (1, '情境甲', '说法甲', 3, 7, '本机学习', NOW - 5_000, 1, NOW - 1_000),
        (2, '情境乙', '说法乙', 5, 0, '', NOW - 9_000, -1, None),
    ]
    assert db.execute(
        'SELECT learn_count, reading, quote FROM expressions ORDER BY id'
    ).fetchall() == [(1, '', ''), (1, '', '')]
    db.execute(
        "INSERT INTO expressions (situation, style, stream_id, created_at) "
        "VALUES ('情境丙', '说法丙', 3, ?)",
        (NOW,),
    )
    assert db.execute(
        'SELECT learn_count, reading, quote FROM expressions WHERE situation = ?',
        ('情境丙',),
    ).fetchone() == (1, '', '')
    db.close()


def test_v37_rewinds_cursor_before_first_message_in_window(
    fixed_clock: None,
) -> None:
    """游标回拨到窗口内第一条消息之前；窗口内没有消息的会话不动，整份 JSON 覆盖写回。"""

    db = _v36_shaped_db({'3': 11, '5': 21, '7': 30})

    v36_to_v37.migrate(db)

    assert v36_to_v37.CURSOR_KEY == CURSOR_KEY
    assert BACKFILL_DAYS == 14
    assert _cursors(db) == {'3': 10, '5': 20, '7': 30}
    assert db.execute(
        'SELECT value FROM meta WHERE key = ?', (CURSOR_KEY,),
    ).fetchone()[0] == json.dumps({'3': 10, '5': 20, '7': 30}, ensure_ascii=False)
    db.close()


def test_v37_keeps_cursor_already_earlier_than_window(fixed_clock: None) -> None:
    """当前游标已经更早（落后于窗口）时不回拨：游标只允许被推早，不允许被推后。"""

    db = _v36_shaped_db({'3': 5, '5': 19})

    v36_to_v37.migrate(db)

    assert _cursors(db) == {'3': 5, '5': 19}
    db.close()


def test_v37_window_includes_message_exactly_at_cutoff(fixed_clock: None) -> None:
    """窗口边界取 >=：正好落在 14 天前的消息进窗口，它前一条消息决定回拨目标。"""

    db = _v36_shaped_db({'9': 92})
    cutoff = NOW - WINDOW_MS
    db.executemany(
        "INSERT INTO messages (id, role, content, created_at, stream_id) "
        "VALUES (?, 'user', '消息', ?, 9)",
        [
            (90, cutoff - 1),
            (91, cutoff),
            (92, cutoff + DAY_MS),
        ],
    )

    v36_to_v37.migrate(db)

    # 边界若误取 >，窗口内最早消息是 92，游标会被回拨到 91。
    assert _cursors(db) == {'9': 90}
    db.close()


def test_v37_replay_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    fixed_clock: None,
) -> None:
    """重放不重复补列、不重复自检、不改已回拨的游标，也不把学习累加的计数改回去。"""

    db = _v36_shaped_db({'3': 11, '5': 21})
    v36_to_v37.migrate(db)
    assert _cursors(db) == {'3': 10, '5': 20}
    # 模拟升级后学习已经运行：learn_count 大于 1 的行在重放时必须让自检保持沉默，
    # 否则正常库会被自检拦成启动故障。
    db.execute('UPDATE expressions SET learn_count = 5 WHERE id = 1')

    monkeypatch.setattr(v36_to_v37, 'current_time', lambda: NOW + 10 * DAY_MS)
    v36_to_v37.migrate(db)

    assert _cursors(db) == {'3': 10, '5': 20}
    assert db.execute(
        'SELECT learn_count FROM expressions WHERE id = 1'
    ).fetchone() == (5,)
    column_names = [str(row[1]) for row in db.execute('PRAGMA table_info(expressions)')]
    assert column_names.count('learn_count') == 1
    db.close()


def test_v37_self_check_rejects_mismatched_column(fixed_clock: None) -> None:
    """本次仍执行了补列时自检必须真的覆盖三列：已存在但声明不符的列要被暴露。"""

    db = _v36_shaped_db(None)
    db.execute('ALTER TABLE expressions ADD COLUMN learn_count INTEGER')

    with pytest.raises(RuntimeError, match='learn_count'):
        v36_to_v37.migrate(db)
    db.close()


def test_v37_warns_and_keeps_cursor_when_value_is_not_object(
    monkeypatch: pytest.MonkeyPatch,
    fixed_clock: None,
) -> None:
    """游标值不是 JSON 对象或 JSON 非法时只告警不抛，游标原样保留。"""

    recorder = _LoggerRecorder()
    db = _v36_shaped_db(None)
    db.execute(
        'INSERT INTO meta (key, value) VALUES (?, ?)', (CURSOR_KEY, '["3"]'),
    )
    monkeypatch.setattr(v36_to_v37, 'logger', recorder)

    v36_to_v37.migrate(db)

    assert 'v36_to_v37_cursor_not_object' in recorder.events
    assert db.execute(
        'SELECT value FROM meta WHERE key = ?', (CURSOR_KEY,),
    ).fetchone() == ('["3"]',)

    db.execute('UPDATE meta SET value = ? WHERE key = ?', ('不是 JSON', CURSOR_KEY))
    v36_to_v37.migrate(db)

    assert 'v36_to_v37_cursor_unparsable' in recorder.events
    assert db.execute(
        'SELECT value FROM meta WHERE key = ?', (CURSOR_KEY,),
    ).fetchone() == ('不是 JSON',)
    db.close()


def test_v37_skips_database_without_expressions_table(fixed_clock: None) -> None:
    """缺 expressions 表的最小库直接跳过：不建表、不回拨游标、不抛。"""

    db = _v36_shaped_db({'3': 11})
    db.execute('DROP TABLE expressions')

    v36_to_v37.migrate(db)

    assert _cursors(db) == {'3': 11}
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'expressions'"
    ).fetchone() is None
    db.close()


def test_v37_leaves_meta_untouched_when_cursor_key_absent(
    fixed_clock: None,
) -> None:
    """游标键不存在时不写 meta：补列照常完成，但不凭空建游标记录。"""

    db = _v36_shaped_db(None)

    v36_to_v37.migrate(db)

    assert db.execute(
        'SELECT COUNT(*) FROM meta WHERE key = ?', (CURSOR_KEY,),
    ).fetchone()[0] == 0
    column_names = [str(row[1]) for row in db.execute('PRAGMA table_info(expressions)')]
    assert {'learn_count', 'reading', 'quote'} <= set(column_names)
    db.close()


def test_chain_from_v36_reaches_current_version(fixed_clock: None) -> None:
    """停在 v36 的库走完整条迁移链后到达链条头，列与回拨后的游标都在。"""

    db = _v36_shaped_db({'3': 11, '5': 21, '7': 30})

    run_migrations(db)

    registry = load_migration_registry()
    assert get_user_version(db) == CURRENT_VERSION
    assert max(registry) + 1 == CURRENT_VERSION
    assert registry[FROM_VERSION] is v36_to_v37.migrate
    assert _cursors(db) == {'3': 10, '5': 20, '7': 30}
    column_names = [str(row[1]) for row in db.execute('PRAGMA table_info(expressions)')]
    assert {'learn_count', 'reading', 'quote'} <= set(column_names)
    db.close()
