"""表情包画面描述列与标签清洗迁移（v35 -> v36）回归。

钉三件事：旧结构的 emoji 表补上 description 列；带模型控制符号的标签被清洗且只有
这些行的向量被置空；重放不改动已清洗的数据；缺表的最小库直接跳过。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.db.migrations.v35_to_v36 import migrate

_LEGACY_EMOJI = '''
CREATE TABLE emoji (
  hash          TEXT PRIMARY KEY,
  visual_key    TEXT    NOT NULL DEFAULT '',
  send_ref      TEXT    NOT NULL,
  emotion_tags  TEXT    NOT NULL,
  emotion_vec   BLOB,
  sub_type      INTEGER NOT NULL DEFAULT 1,
  seen_count    INTEGER NOT NULL DEFAULT 1,
  use_count     INTEGER NOT NULL DEFAULT 0,
  last_used_at  INTEGER,
  first_seen_at INTEGER NOT NULL
);
'''


def _legacy_db() -> sqlite3.Connection:
    db = sqlite3.connect(':memory:')
    db.executescript(_LEGACY_EMOJI)
    rows = [
        ('a' * 64, '调侃,戏谑,幽默<|end_of_box|>'),
        ('b' * 64, '可爱,温柔,萌</think>'),
        ('c' * 64, '无语,无奈'),
    ]
    for content_hash, tags in rows:
        db.execute(
            'INSERT INTO emoji (hash, send_ref, emotion_tags, emotion_vec, first_seen_at) '
            "VALUES (?, 'file:///x.png', ?, x'00000000', 1)",
            (content_hash, tags),
        )
    return db


def _row(db: sqlite3.Connection, content_hash: str) -> tuple:
    return db.execute(
        'SELECT emotion_tags, emotion_vec, description FROM emoji WHERE hash = ?', (content_hash,),
    ).fetchone()


def test_v36_adds_description_and_cleans_tags() -> None:
    db = _legacy_db()

    migrate(db)

    assert _row(db, 'a' * 64) == ('调侃,戏谑,幽默', None, '')
    assert _row(db, 'b' * 64) == ('可爱,温柔,萌', None, '')
    # 干净的行连向量一起保留。
    assert _row(db, 'c' * 64) == ('无语,无奈', b'\x00\x00\x00\x00', '')


def test_v36_replay_is_noop_and_missing_table_skips() -> None:
    db = _legacy_db()
    migrate(db)
    db.execute("UPDATE emoji SET emotion_vec = x'01000000' WHERE hash = ?", ('a' * 64,))

    migrate(db)

    assert _row(db, 'a' * 64)[1] == b'\x01\x00\x00\x00'
    migrate(sqlite3.connect(':memory:'))


def test_v36_rejects_tags_that_clean_to_nothing() -> None:
    db = _legacy_db()
    db.execute("UPDATE emoji SET emotion_tags = '<|end_of_box|>' WHERE hash = ?", ('c' * 64,))

    with pytest.raises(RuntimeError, match='清洗后为空'):
        migrate(db)
