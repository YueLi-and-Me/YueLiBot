"""
v5 → v6 迁移：为记忆和人格状态建立 stream / person 分区。

facts_fts 是 contentless FTS5，rowid 必须一直等于 facts.id。因此 facts 重建时显式
搬运 id，绝不能重建或重写 facts_fts；否则召回会静默指向错误的事实。
"""

from __future__ import annotations

import sqlite3

from .registry import register

from src.common.logger import get_logger

logger = get_logger(__name__)

OWNER_PERSON_ID = 1
DESKTOP_STREAM_ID = 1
_DESKTOP_EXTERNAL_ID = "desktop"


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _read_owner_persona(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute(
        """SELECT intimacy, tsundere, reliance, energy, updated_at
           FROM persona WHERE id = 1"""
    ).fetchone()
    if row is None:
        raise RuntimeError("v5 数据库缺少 owner persona 行（id=1）")
    return row


def _read_first_seen_at(db: sqlite3.Connection) -> int:
    row = db.execute(
        "SELECT value FROM meta WHERE key = 'first_seen_at'"
    ).fetchone()
    if row is None:
        raise RuntimeError("v5 数据库缺少 meta.first_seen_at")
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("v5 数据库的 meta.first_seen_at 不是整数") from exc


def _create_ownership_tables(db: sqlite3.Connection) -> None:
    """创建 v6 新表；此函数只使用 execute，保持迁移整体可回滚。"""
    db.execute(
        """CREATE TABLE IF NOT EXISTS persons (
               id            INTEGER PRIMARY KEY,
               kind          TEXT    NOT NULL,
               first_seen_at INTEGER NOT NULL
           )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS streams (
               id          INTEGER PRIMARY KEY,
               platform    TEXT    NOT NULL,
               kind        TEXT    NOT NULL,
               external_id TEXT    NOT NULL,
               UNIQUE(platform, kind, external_id)
           )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS identities (
               person_id    INTEGER NOT NULL REFERENCES persons(id),
               platform     TEXT    NOT NULL,
               external_id  TEXT    NOT NULL,
               display_name TEXT    NOT NULL,
               PRIMARY KEY(platform, external_id)
           )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_identities_person ON identities(person_id)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS persona_bond (
               person_id  INTEGER PRIMARY KEY REFERENCES persons(id),
               intimacy   REAL    NOT NULL,
               tsundere   REAL    NOT NULL,
               reliance   REAL    NOT NULL,
               updated_at INTEGER NOT NULL
           )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS persona_self (
               id         INTEGER PRIMARY KEY CHECK (id = 1),
               energy     REAL    NOT NULL,
               updated_at INTEGER NOT NULL
           )"""
    )


def _add_partition_columns(db: sqlite3.Connection) -> None:
    message_columns = _table_columns(db, "messages")
    if "stream_id" not in message_columns:
        db.execute("ALTER TABLE messages ADD COLUMN stream_id INTEGER NOT NULL DEFAULT 1")
    if "sender_person_id" not in message_columns:
        db.execute("ALTER TABLE messages ADD COLUMN sender_person_id INTEGER")
    db.execute(
        """UPDATE messages SET sender_person_id = ?
           WHERE role = 'user' AND sender_person_id IS NULL""",
        (OWNER_PERSON_ID,),
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS idx_messages_stream_pending
           ON messages(stream_id, episode_id, id)"""
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS idx_messages_stream_created
           ON messages(stream_id, created_at)"""
    )

    episode_columns = _table_columns(db, "episodes")
    if "stream_id" not in episode_columns:
        # episodes 被 messages 和 episode_cues 引用，只能 ADD COLUMN，不能重建。
        db.execute("ALTER TABLE episodes ADD COLUMN stream_id INTEGER NOT NULL DEFAULT 1")
    db.execute(
        """CREATE INDEX IF NOT EXISTS idx_episodes_stream_time
           ON episodes(stream_id, ended_at DESC)"""
    )


def _rebuild_facts(db: sqlite3.Connection) -> None:
    if "person_id" in _table_columns(db, "facts"):
        return

    # 不能给 facts_fts 做任何 DROP / CREATE / INSERT。它的 rowid 仍指向旧 facts.id，
    # 所以 INSERT ... SELECT 的第一列必须保留原 id。
    db.execute(
        """CREATE TABLE facts_v6 (
               id              INTEGER PRIMARY KEY,
               person_id       INTEGER NOT NULL DEFAULT 1,
               kind            TEXT    NOT NULL DEFAULT '未分类',
               content         TEXT    NOT NULL,
               content_key     TEXT    NOT NULL,
               strength        REAL    NOT NULL,
               half_life_hours REAL    NOT NULL,
               updated_at      INTEGER NOT NULL,
               created_at      INTEGER NOT NULL,
               hit_count       INTEGER NOT NULL DEFAULT 0,
               last_hit_at     INTEGER,
               due_at          INTEGER NOT NULL,
               active          INTEGER NOT NULL DEFAULT 1,
               tokens_v2       TEXT    NOT NULL DEFAULT '',
               embedding       BLOB,
               UNIQUE(person_id, content_key)
           )"""
    )
    db.execute(
        """INSERT INTO facts_v6 (
               id, person_id, kind, content, content_key, strength, half_life_hours,
               updated_at, created_at, hit_count, last_hit_at, due_at, active,
               tokens_v2, embedding
           )
           SELECT
               id, ?, kind, content, content_key, strength, half_life_hours,
               updated_at, created_at, hit_count, last_hit_at, due_at, active,
               tokens_v2, embedding
           FROM facts""",
        (OWNER_PERSON_ID,),
    )
    db.execute("DROP TABLE facts")
    db.execute("ALTER TABLE facts_v6 RENAME TO facts")
    db.execute("CREATE INDEX idx_facts_due ON facts(active, due_at)")
    db.execute("CREATE INDEX idx_facts_person_active ON facts(person_id, active)")


def _write_owner_records(
    db: sqlite3.Connection,
    persona: sqlite3.Row,
    first_seen_at: int,
) -> None:
    db.execute(
        """INSERT INTO persons (id, kind, first_seen_at) VALUES (?, 'owner', ?)
           ON CONFLICT(id) DO UPDATE SET
               kind = excluded.kind,
               first_seen_at = excluded.first_seen_at""",
        (OWNER_PERSON_ID, first_seen_at),
    )
    db.execute(
        """INSERT INTO streams (id, platform, kind, external_id)
           VALUES (?, 'desktop', 'desktop', ?)
           ON CONFLICT(id) DO UPDATE SET
               platform = excluded.platform,
               kind = excluded.kind,
               external_id = excluded.external_id""",
        (DESKTOP_STREAM_ID, _DESKTOP_EXTERNAL_ID),
    )
    db.execute(
        """INSERT INTO persona_bond (
               person_id, intimacy, tsundere, reliance, updated_at
           ) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(person_id) DO UPDATE SET
               intimacy = excluded.intimacy,
               tsundere = excluded.tsundere,
               reliance = excluded.reliance,
               updated_at = excluded.updated_at""",
        (
            OWNER_PERSON_ID,
            persona["intimacy"],
            persona["tsundere"],
            persona["reliance"],
            persona["updated_at"],
        ),
    )
    db.execute(
        """INSERT INTO persona_self (id, energy, updated_at) VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               energy = excluded.energy,
               updated_at = excluded.updated_at""",
        (persona["energy"], persona["updated_at"]),
    )


def _assert_migration_integrity(db: sqlite3.Connection) -> None:
    nullable_columns = (
        ("messages", "stream_id"),
        ("episodes", "stream_id"),
        ("facts", "person_id"),
    )
    for table, column in nullable_columns:
        row = db.execute(
            f"SELECT 1 FROM {table} WHERE {column} IS NULL LIMIT 1"
        ).fetchone()
        if row is not None:
            raise RuntimeError(f"v6 迁移自检失败：{table}.{column} 存在空值")

    missing_sender = db.execute(
        """SELECT 1 FROM messages
           WHERE role = 'user' AND sender_person_id IS NULL LIMIT 1"""
    ).fetchone()
    if missing_sender is not None:
        raise RuntimeError("v6 迁移自检失败：存在没有 sender_person_id 的 user 消息")

    self_rows = db.execute("SELECT id FROM persona_self").fetchall()
    if len(self_rows) != 1 or self_rows[0][0] != 1:
        raise RuntimeError("v6 迁移自检失败：persona_self 必须恰好有 id=1 的一行")

    foreign_key_rows = db.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_rows:
        raise RuntimeError("v6 迁移自检失败：外键完整性检查失败")

    integrity_rows = db.execute("PRAGMA integrity_check").fetchall()
    if [row[0] for row in integrity_rows] != ["ok"]:
        raise RuntimeError("v6 迁移自检失败：数据库完整性检查失败")


@register(5)
def v5_to_v6(db: sqlite3.Connection) -> None:
    """把单桌面 v5 数据归位到 owner person 与 desktop stream。"""
    # 在创建目标表或写入默认值之前读旧真相；缺失即抛错，避免把损坏库伪装成新用户。
    persona = _read_owner_persona(db)
    first_seen_at = _read_first_seen_at(db)

    _create_ownership_tables(db)
    _add_partition_columns(db)
    _rebuild_facts(db)
    _write_owner_records(db, persona, first_seen_at)
    _assert_migration_integrity(db)
    logger.info("v5_to_v6_done", owner_person_id=OWNER_PERSON_ID, desktop_stream_id=DESKTOP_STREAM_ID)
