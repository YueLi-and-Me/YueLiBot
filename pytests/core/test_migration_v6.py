"""v5 → v6 分区迁移的文件库回归。

本模块覆盖一次性迁移的安全性和备份行为，运行结果不应绕过 .gitignore 写入版本控制。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.core.db.migrations.manager import CURRENT_VERSION, run_migrations


FIRST_SEEN_AT = 1_700_000_000_000
NOW = 1_800_000_000_000




def _row_count(db: sqlite3.Connection, table: str) -> int:
    """按固定分支读取表行数；PRAGMA/标识位都不支持参数绑定，整句字面量是唯一安全形态。"""
    if table == "messages":
        return db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if table == "episodes":
        return db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    if table == "facts":
        return db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    if table == "persona_snapshots":
        return db.execute("SELECT COUNT(*) FROM persona_snapshots").fetchone()[0]
    raise ValueError(f"未登记的表名：{table}")


def _build_v5_database(path: Path) -> None:
    """构造包含稀疏 FTS rowid 的真实 v5 文件库。"""
    db = sqlite3.connect(path)
    db.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE messages (
          id INTEGER PRIMARY KEY,
          role TEXT NOT NULL,
          content TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          episode_id INTEGER REFERENCES episodes(id) ON DELETE SET NULL
        );

        CREATE TABLE episodes (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL DEFAULT 'conversation',
          summary TEXT NOT NULL,
          started_at INTEGER NOT NULL,
          ended_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL
        );

        CREATE TABLE episode_cues (
          id INTEGER PRIMARY KEY,
          episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
          cue TEXT NOT NULL
        );

        CREATE TABLE facts (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL DEFAULT '未分类',
          content TEXT NOT NULL,
          content_key TEXT NOT NULL UNIQUE,
          strength REAL NOT NULL,
          half_life_hours REAL NOT NULL,
          updated_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL,
          hit_count INTEGER NOT NULL DEFAULT 0,
          last_hit_at INTEGER,
          due_at INTEGER NOT NULL,
          active INTEGER NOT NULL DEFAULT 1,
          tokens_v2 TEXT NOT NULL DEFAULT '',
          embedding BLOB
        );

        CREATE VIRTUAL TABLE facts_fts USING fts5(
          tokens, content='', tokenize='unicode61'
        );
        CREATE VIRTUAL TABLE cues_fts USING fts5(
          tokens, content='', tokenize='unicode61'
        );

        CREATE TABLE pending_utterances (
          id INTEGER PRIMARY KEY,
          source TEXT NOT NULL,
          emotion TEXT,
          text TEXT NOT NULL,
          deliver_after INTEGER NOT NULL,
          expires_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL,
          delivered_at INTEGER
        );

        CREATE TABLE persona (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          intimacy REAL NOT NULL,
          tsundere REAL NOT NULL,
          reliance REAL NOT NULL,
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
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [
            ("schema_version", "3"),
            ("first_seen_at", str(FIRST_SEEN_AT)),
        ],
    )
    db.execute(
        "INSERT INTO persona VALUES (1, ?, ?, ?, ?, ?)",
        (67.0, -8.0, 41.0, 73.0, NOW),
    )
    db.execute(
        "INSERT INTO persona_snapshots VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-08-04", 66.0, -7.0, 40.0, 74.0, NOW - 86_400_000),
    )
    db.execute(
        "INSERT INTO episodes VALUES (11, 'conversation', '聊了咖啡', ?, ?, ?)",
        (NOW - 2_000, NOW - 1_000, NOW),
    )
    db.executemany(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
        [
            (3, "user", "我喜欢咖啡", NOW - 2_000, 11),
            (8, "assistant", "记住了", NOW - 1_500, 11),
            (15, "user", "今天好忙", NOW, None),
        ],
    )
    db.execute("INSERT INTO episode_cues VALUES (13, 11, '咖啡 偏好')")
    db.execute("INSERT INTO cues_fts(rowid, tokens) VALUES (13, '咖啡 偏好')")
    db.executemany(
        """INSERT INTO facts (
               id, kind, content, content_key, strength, half_life_hours,
               updated_at, created_at, hit_count, last_hit_at, due_at, active,
               tokens_v2, embedding
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                7,
                "偏好",
                "用户喜欢咖啡",
                "用户喜欢咖啡",
                0.9,
                720.0,
                NOW - 2_000,
                NOW - 3_000,
                4,
                NOW - 1_000,
                NOW + 3_600_000,
                1,
                "用户 喜欢 咖啡",
                b"coffee-vector",
            ),
            (
                42,
                "宠物",
                "用户养了一只猫",
                "用户养了一只猫",
                0.8,
                360.0,
                NOW - 4_000,
                NOW - 5_000,
                2,
                None,
                NOW + 7_200_000,
                1,
                "用户 养 猫",
                b"cat-vector",
            ),
        ],
    )
    db.executemany(
        "INSERT INTO facts_fts(rowid, tokens) VALUES (?, ?)",
        [(7, "用户 喜欢 咖啡"), (42, "用户 养 猫")],
    )
    db.execute("PRAGMA user_version = 5")
    db.commit()
    db.close()


def _open(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def test_v5_migration_preserves_partitioned_data_and_fts_rowids(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    _build_v5_database(path)

    db = _open(path)
    run_migrations(db, path)

    assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    assert db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'group_memberships'"
    ).fetchone()[0] == 'group_memberships'
    assert dict(db.execute("SELECT id, kind, first_seen_at FROM persons WHERE id = 1").fetchone()) == {
        "id": 1,
        "kind": "owner",
        "first_seen_at": FIRST_SEEN_AT,
    }
    assert dict(db.execute("SELECT id, platform, kind, external_id FROM streams WHERE id = 1").fetchone()) == {
        "id": 1,
        "platform": "desktop",
        "kind": "desktop",
        "external_id": "desktop",
    }
    assert dict(db.execute("SELECT intimacy FROM persona_bond WHERE person_id = 1").fetchone()) == {
        "intimacy": 67.0,
    }
    assert dict(db.execute("SELECT id, energy FROM persona_self").fetchone()) == {
        "id": 1,
        "energy": 73.0,
    }

    messages = db.execute(
        "SELECT id, role, stream_id, sender_person_id FROM messages ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in messages] == [
        (3, "user", 1, 1),
        (8, "assistant", 1, None),
        (15, "user", 1, 1),
    ]
    assert db.execute("SELECT stream_id FROM episodes WHERE id = 11").fetchone()[0] == 1
    assert [tuple(row) for row in db.execute(
        "SELECT id, person_id, tokens_v2, embedding FROM facts ORDER BY id"
    ).fetchall()] == [
        (7, 1, "用户 喜欢 咖啡", b"coffee-vector"),
        (42, 1, "用户 养 猫", b"cat-vector"),
    ]
    assert db.execute("SELECT COUNT(*) FROM persona_snapshots").fetchone()[0] == 1

    fts_rows = db.execute(
        """SELECT facts_fts.rowid, f.id, f.content
           FROM facts_fts
           JOIN facts AS f ON f.id = facts_fts.rowid
           WHERE facts_fts MATCH ?""",
        ("咖啡",),
    ).fetchall()
    assert [tuple(row) for row in fts_rows] == [(7, 7, "用户喜欢咖啡")]

    counts_before = {
        table: _row_count(db, table)
        for table in ("messages", "episodes", "facts", "persona_snapshots")
    }
    run_migrations(db, path)
    counts_after = {
        table: _row_count(db, table)
        for table in ("messages", "episodes", "facts", "persona_snapshots")
    }
    assert counts_after == counts_before
    assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    db.close()

    backups = list((tmp_path / "backups").glob("memory.v5.*.db"))
    assert len(backups) == 1
    backup = _open(backups[0])
    assert backup.execute("PRAGMA user_version").fetchone()[0] == 5
    assert backup.execute("SELECT content FROM facts WHERE id = 42").fetchone()[0] == "用户养了一只猫"
    backup.close()


def test_v5_migration_rolls_back_when_owner_persona_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    _build_v5_database(path)
    db = _open(path)
    db.execute("DELETE FROM persona WHERE id = 1")
    db.commit()

    with pytest.raises(RuntimeError, match="persona"):
        run_migrations(db, path)

    assert db.execute("PRAGMA user_version").fetchone()[0] == 5
    columns = {row[1] for row in db.execute("PRAGMA table_info(facts)").fetchall()}
    assert "person_id" not in columns
    assert db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 2
    db.close()
