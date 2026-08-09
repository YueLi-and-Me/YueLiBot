"""
SQLite schema DDL。

直接从 src/core/memory/schema.ts 移植，保留所有注释与索引。
Python 侧不使用 ORM —— contentless FTS5 虚表和手工维护的索引
在 SQLAlchemy 里需要大量 text() 绕路，不如直接用 sqlite3。
"""

SCHEMA_VERSION = 3

DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ---------------------------------------------------------------- stream / person 归属
CREATE TABLE IF NOT EXISTS persons (
  id            INTEGER PRIMARY KEY,
  kind          TEXT    NOT NULL,
  first_seen_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS streams (
  id          INTEGER PRIMARY KEY,
  platform    TEXT    NOT NULL,
  kind        TEXT    NOT NULL,
  external_id TEXT    NOT NULL,
  UNIQUE(platform, kind, external_id)
);

CREATE TABLE IF NOT EXISTS identities (
  person_id   INTEGER NOT NULL REFERENCES persons(id),
  platform    TEXT    NOT NULL,
  external_id TEXT    NOT NULL,
  display_name TEXT   NOT NULL,
  PRIMARY KEY(platform, external_id)
);
CREATE INDEX IF NOT EXISTS idx_identities_person ON identities(person_id);

-- QQ 账号昵称属于 identity；群名片属于 person 在具体群 stream 中的可变属性。
CREATE TABLE IF NOT EXISTS group_memberships (
  stream_id   INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
  person_id   INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  group_card  TEXT    NOT NULL,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY(stream_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_group_memberships_person ON group_memberships(person_id);

-- ---------------------------------------------------------------- L1 工作记忆
CREATE TABLE IF NOT EXISTS messages (
  id               INTEGER PRIMARY KEY,
  role             TEXT    NOT NULL,
  content          TEXT    NOT NULL,
  created_at       INTEGER NOT NULL,
  episode_id       INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
  stream_id        INTEGER NOT NULL DEFAULT 1,
  sender_person_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_pending ON messages(episode_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_stream_pending ON messages(stream_id, episode_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_stream_created ON messages(stream_id, created_at);

-- ---------------------------------------------------------------- L2 情节记忆
CREATE TABLE IF NOT EXISTS episodes (
  id         INTEGER PRIMARY KEY,
  kind       TEXT    NOT NULL DEFAULT 'conversation',
  summary    TEXT    NOT NULL,
  started_at INTEGER NOT NULL,
  ended_at   INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  stream_id  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_episodes_time ON episodes(ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_stream_time ON episodes(stream_id, ended_at DESC);

CREATE TABLE IF NOT EXISTS episode_cues (
  id         INTEGER PRIMARY KEY,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  cue        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cues_episode ON episode_cues(episode_id);

-- ---------------------------------------------------------------- L3 语义记忆
CREATE TABLE IF NOT EXISTS facts (
  id              INTEGER PRIMARY KEY,
  -- facts 有意不带 stream_id：群级事实留到 v7 再单独设计迁移。
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
  tokens_v2       TEXT    NOT NULL DEFAULT '',   -- jieba 预分词（v4 加）
  embedding       BLOB,                          -- float32 packed（v5 加）
  UNIQUE(person_id, content_key)
);
CREATE INDEX IF NOT EXISTS idx_facts_due ON facts(active, due_at);
CREATE INDEX IF NOT EXISTS idx_facts_person_active ON facts(person_id, active);

-- ---------------------------------------------------------------- 全文索引
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(tokens, content='', tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS cues_fts  USING fts5(tokens, content='', tokenize='unicode61');

-- ---------------------------------------------------------------- 待说的话
CREATE TABLE IF NOT EXISTS pending_utterances (
  id            INTEGER PRIMARY KEY,
  source        TEXT    NOT NULL,
  emotion       TEXT,
  text          TEXT    NOT NULL,
  deliver_after INTEGER NOT NULL,
  expires_at    INTEGER NOT NULL,
  created_at    INTEGER NOT NULL,
  delivered_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pending_due ON pending_utterances(delivered_at, deliver_after);

-- ---------------------------------------------------------------- 人格状态
-- 好感度按人保存；person_id=1 是 owner，其他 person 在首次交互时懒创建。
CREATE TABLE IF NOT EXISTS persona_bond (
  person_id  INTEGER PRIMARY KEY REFERENCES persons(id),
  intimacy   REAL    NOT NULL,
  updated_at INTEGER NOT NULL
);

-- energy 是她唯一的身体状态，保留单行 CHECK(id=1) 是正确建模，不是历史遗留。
CREATE TABLE IF NOT EXISTS persona_self (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  energy     REAL    NOT NULL,
  updated_at INTEGER NOT NULL
);

-- M1.4.4 前由旧 Persona 继续读写；之后只作为 owner 的历史迁移留存，不再写入。
CREATE TABLE IF NOT EXISTS persona (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  intimacy   REAL    NOT NULL,
  tsundere   REAL    NOT NULL,
  reliance   REAL    NOT NULL,
  energy     REAL    NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_snapshots (
  date       TEXT PRIMARY KEY,
  intimacy   REAL    NOT NULL,
  energy     REAL    NOT NULL,
  captured_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_persona_snapshots_time ON persona_snapshots(captured_at DESC);
"""

SEED = """
INSERT OR IGNORE INTO persons (id, kind, first_seen_at)
VALUES (1, 'owner', CAST(strftime('%s','now') AS INTEGER) * 1000);

INSERT OR IGNORE INTO streams (id, platform, kind, external_id)
VALUES (1, 'desktop', 'desktop', 'desktop');

INSERT OR IGNORE INTO persona_bond (person_id, intimacy, updated_at)
VALUES (1, 12, CAST(strftime('%s','now') AS INTEGER) * 1000);

INSERT OR IGNORE INTO persona_self (id, energy, updated_at)
VALUES (1, 80, CAST(strftime('%s','now') AS INTEGER) * 1000);

INSERT OR IGNORE INTO persona (id, intimacy, tsundere, reliance, energy, updated_at)
VALUES (1, 12, 5, 20, 80, CAST(strftime('%s','now') AS INTEGER) * 1000);
"""
