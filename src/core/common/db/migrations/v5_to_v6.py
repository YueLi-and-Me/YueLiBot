"""
v5 → v6 迁移：为记忆和人格状态建立 stream / person 分区。

facts_fts 是 contentless FTS5，rowid 必须一直等于 facts.id。因此 facts 重建时显式
搬运 id，绝不能重建或重写 facts_fts；否则召回会静默指向错误的事实。
"""

from __future__ import annotations

import sqlite3

from .registry import register

from src.core.common.logger import get_logger

logger = get_logger(__name__)

OWNER_PERSON_ID = 1
DESKTOP_STREAM_ID = 1
_DESKTOP_EXTERNAL_ID = "desktop"


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    """读取指定表的列名集合。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :param table: 要检查的表名，必须来自固定迁移 SQL。
    :return: 表结构中的列名集合。
    :raises sqlite3.Error: 表结构查询失败时抛出。
    副作用：只读 SQLite 表结构。
    """
    # 表值函数形态的 PRAGMA 支持参数绑定，表名走占位符而不是拼进 SQL 文本。
    rows = db.execute('SELECT name FROM pragma_table_info(?)', (table,)).fetchall()
    return {str(row[0]) for row in rows}


def _read_owner_persona(db: sqlite3.Connection) -> sqlite3.Row:
    """读取 v5 数据库中 owner 的人格状态行。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :return: 包含 intimacy、tsundere、reliance、energy 和 updated_at 的行。
    :raises RuntimeError: 缺少 `persona.id=1` 行。
    :raises sqlite3.Error: 查询失败时抛出。
    副作用：只读旧人格表。
    """
    row = db.execute(
        """SELECT intimacy, tsundere, reliance, energy, updated_at
           FROM persona WHERE id = 1"""
    ).fetchone()
    if row is None:
        raise RuntimeError("v5 数据库缺少 owner persona 行（id=1）")
    return row


def _read_first_seen_at(db: sqlite3.Connection) -> int:
    """读取并解析旧库保存的 owner 首次出现时间。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :return: `meta.first_seen_at` 的整数时间戳。
    :raises RuntimeError: 字段缺失或不是整数。
    :raises sqlite3.Error: 查询失败时抛出。
    副作用：只读 `meta` 表。
    """
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
    """创建 v6 的人物、stream、identity 和人格分区表。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :return: 无返回值。
    副作用：在当前事务中创建缺失表和索引；不提交事务。
    :raises sqlite3.Error: DDL 执行失败时抛出。
    """
    # 先创建被外键引用的主体表，再创建身份、关系和状态表，避免迁移顺序依赖隐式行为。
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
    """为消息和情节表补充 stream/person 分区列及索引。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :return: 无返回值。
    副作用：可能修改 `messages`、`episodes` 的结构和数据，并创建索引；不提交事务。
    :raises sqlite3.Error: DDL、DML 或索引创建失败时抛出。
    """
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
    """把 facts 重建为带 person_id 的 v6 结构并保留原始主键。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :return: 无返回值；已含 `person_id` 列时直接返回。
    副作用：创建临时事实表、复制数据、替换旧表并重建普通索引；不操作 FTS 表，
        不提交事务。
    :raises sqlite3.Error: 表替换、数据复制或索引创建失败时抛出。
    """
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
    """写入 owner person、desktop stream、人格关系和精力记录。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :param persona: 从旧 `persona` 表读取的人格状态行。
    :param first_seen_at: owner 首次出现的 Unix 毫秒时间戳。
    :return: 无返回值。
    副作用：插入或更新人物归属、desktop stream 和两张人格表；不提交事务。
    :raises sqlite3.Error: 目标表写入失败时抛出。
    """
    # 使用固定主键写入 owner 与 desktop stream，使历史消息的默认分区保持稳定。
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
    """验证 v5 到 v6 迁移后的关键约束。

    :param db: 当前迁移事务使用的 SQLite 连接。
    :return: 所有检查通过时返回 `None`。
    :raises RuntimeError: 分区列存在空值、用户消息缺少 owner、精力行数错误、外键或
        SQLite 完整性检查失败。
    副作用：只读迁移后的表和 SQLite 检查结果。
    """
    nullable_columns = (
        ("messages", "stream_id"),
        ("episodes", "stream_id"),
        ("facts", "person_id"),
    )
    # 分区列必须全部有值，否则后续按 stream/person 查询会静默漏数据。
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

    # 最后执行外键和 SQLite 完整性检查，确保结构替换没有留下悬空引用。
    foreign_key_rows = db.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_rows:
        raise RuntimeError("v6 迁移自检失败：外键完整性检查失败")

    integrity_rows = db.execute("PRAGMA integrity_check").fetchall()
    if [row[0] for row in integrity_rows] != ["ok"]:
        raise RuntimeError("v6 迁移自检失败：数据库完整性检查失败")


@register(5)
def v5_to_v6(db: sqlite3.Connection) -> None:
    """将单桌面 v5 数据迁移到 owner person 和 desktop stream 分区。

    :param db: 当前迁移事务使用的 SQLite 连接。

    :raises RuntimeError: 旧库缺少 owner 人格、首次出现时间或迁移后完整性校验失败。
    :raises sqlite3.Error: 分区表创建、数据复制、索引创建或完整性检查失败。

    副作用：
        创建人物、stream、identity 和人格分区表，补充消息与情节分区列，重建
        ``facts`` 表并保留原始主键，写入 owner 记录；不提交事务。
    """
    # 在创建目标表或写入默认值之前读旧真相；缺失即抛错，避免把损坏库伪装成新用户。
    persona = _read_owner_persona(db)
    first_seen_at = _read_first_seen_at(db)

    _create_ownership_tables(db)
    _add_partition_columns(db)
    _rebuild_facts(db)
    _write_owner_records(db, persona, first_seen_at)
    _assert_migration_integrity(db)
    logger.info("v5_to_v6_done", owner_person_id=OWNER_PERSON_ID, desktop_stream_id=DESKTOP_STREAM_ID)
