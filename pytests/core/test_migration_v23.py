"""存量事实 slot 回填迁移（v22 -> v23）回归。"""

from __future__ import annotations

import sqlite3

from src.core.common.db.migrations.bootstrap import write_user_version
from src.core.common.db.migrations.manager import (
    CURRENT_VERSION,
    get_user_version,
    run_migrations,
)
from src.core.common.db.migrations.v22_to_v23 import (
    FROM_VERSION,
    classify_slot,
    migrate,
)


def _old_shape(db: sqlite3.Connection) -> None:
    """创建 v22 形态的最小库：facts 带账本两列，slot 全部为空。"""

    db.executescript(
        """
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL,
          created_at INTEGER NOT NULL, episode_id INTEGER, stream_id INTEGER NOT NULL DEFAULT 1,
          sender_person_id INTEGER, external_message_id TEXT
        );
        CREATE TABLE facts (
          id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL DEFAULT 1,
          kind TEXT NOT NULL DEFAULT '事件', content TEXT NOT NULL,
          content_key TEXT NOT NULL, strength REAL NOT NULL,
          half_life_hours REAL NOT NULL, updated_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL, hit_count INTEGER NOT NULL DEFAULT 0,
          last_hit_at INTEGER, due_at INTEGER NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, tokens_v2 TEXT NOT NULL DEFAULT '',
          embedding BLOB, embedding_q8 BLOB, origin_kind TEXT NOT NULL DEFAULT 'legacy',
          slot TEXT NOT NULL DEFAULT '', superseded_by INTEGER REFERENCES facts(id),
          UNIQUE(person_id, content_key)
        );
        """
    )


def _add_fact(db: sqlite3.Connection, person_id: int, kind: str, content: str) -> int:
    cur = db.execute(
        '''INSERT INTO facts (person_id, kind, content, content_key, strength,
                              half_life_hours, updated_at, created_at, due_at)
           VALUES (?, ?, ?, ?, 0.8, 2160, 1, 1, 2)''',
        (person_id, kind, content, content.strip()),
    )
    return int(cur.lastrowid)


def test_classify_slot_hits_single_value_dimensions() -> None:
    """正文明说的单值维度归类：居住地、职业、昵称、年级、生日。"""

    assert classify_slot('凌白住在深圳宝安区') == '居住地'
    assert classify_slot('凌白的祖籍为山东临城，现居或关联地为河南平顶山') == '居住地'
    assert classify_slot('是一名学生，日常需要上自习课。') == '职业'
    assert classify_slot('900000001 是程序员') == '职业'
    assert classify_slot('来品猹 即将开学，是在校学生。') == '职业'
    assert classify_slot('2294904033的昵称是南浊。') == '昵称'
    assert classify_slot('iceyback 自称名为「玖璃」') == '昵称'
    assert classify_slot('3209184542 目前读大三') == '年级'
    assert classify_slot('她的生日是 3 月 14 日') == '生日'


def test_classify_slot_leaves_ambiguous_content_empty() -> None:
    """外号、多值喜好与不含单值维度的正文一律保持空槽。"""

    assert classify_slot('@1624606785 被南浊北渊称为“群里唯一的猪”') == ''
    assert classify_slot('他喜欢喝冰美式') == ''
    assert classify_slot('凌白具备排查和修复AI对话系统的能力') == ''
    assert classify_slot('') == ''


def test_v23_backfills_only_four_kinds_and_keeps_existing_slots() -> None:
    """只有 身份/状态/关系/日期 四类被回填；已有槽位的行不被覆盖。"""

    db = sqlite3.connect(':memory:')
    _old_shape(db)
    a = _add_fact(db, 1, '身份', '凌白住在深圳宝安区')
    b = _add_fact(db, 1, '偏好', '他喜欢住在海边的感觉')   # 多值类不动
    c = _add_fact(db, 1, '事件', '他昨天住在酒店')          # 多值类不动
    d = _add_fact(db, 1, '身份', '是一名学生')
    e = _add_fact(db, 1, '身份', '她现在住在杭州')
    db.execute("UPDATE facts SET slot = '居住地' WHERE id = ?", (e,))  # 抽取已填槽

    migrate(db)
    db.commit()

    slot_of = lambda fid: db.execute('SELECT slot FROM facts WHERE id = ?', (fid,)).fetchone()[0]
    assert slot_of(a) == '居住地'
    assert slot_of(b) == ''
    assert slot_of(c) == ''
    assert slot_of(d) == '职业'
    assert slot_of(e) == '居住地'  # 抽取填的槽位不被回填覆盖

    # 重放幂等：结果不变。
    migrate(db)
    db.commit()
    assert slot_of(a) == '居住地' and slot_of(d) == '职业'
    db.close()


def test_v23_accepts_early_database_without_facts_table() -> None:
    """早期最小库尚无 facts 时不猜结构，交给链尾当前 DDL 创建。"""

    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE legacy_probe (id INTEGER PRIMARY KEY)')

    migrate(db)

    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'facts'"
    ).fetchone()[0] == 0
    db.close()


def test_migration_chain_reaches_declared_head() -> None:
    """迁移管理器从本迁移的入口版本跑到当前链头，不在测试写死版本号。"""

    db = sqlite3.connect(':memory:')
    _old_shape(db)
    write_user_version(db, FROM_VERSION)

    run_migrations(db)

    assert get_user_version(db) == CURRENT_VERSION
    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'memory_feedback_pending'"
    ).fetchone()[0] == 1
    db.close()
