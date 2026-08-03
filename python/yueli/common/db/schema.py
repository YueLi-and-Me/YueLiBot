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

-- ---------------------------------------------------------------- L1 工作记忆
CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY,
  role       TEXT    NOT NULL,
  content    TEXT    NOT NULL,
  created_at INTEGER NOT NULL,
  episode_id INTEGER REFERENCES episodes(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_pending ON messages(episode_id, id);

-- ---------------------------------------------------------------- L2 情节记忆
CREATE TABLE IF NOT EXISTS episodes (
  id         INTEGER PRIMARY KEY,
  kind       TEXT    NOT NULL DEFAULT 'conversation',
  summary    TEXT    NOT NULL,
  started_at INTEGER NOT NULL,
  ended_at   INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_time ON episodes(ended_at DESC);

CREATE TABLE IF NOT EXISTS episode_cues (
  id         INTEGER PRIMARY KEY,
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  cue        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cues_episode ON episode_cues(episode_id);

-- ---------------------------------------------------------------- L3 语义记忆
CREATE TABLE IF NOT EXISTS facts (
  id              INTEGER PRIMARY KEY,
  kind            TEXT    NOT NULL DEFAULT '未分类',
  content         TEXT    NOT NULL,
  content_key     TEXT    NOT NULL UNIQUE,
  strength        REAL    NOT NULL,
  half_life_hours REAL    NOT NULL,
  updated_at      INTEGER NOT NULL,
  created_at      INTEGER NOT NULL,
  hit_count       INTEGER NOT NULL DEFAULT 0,
  last_hit_at     INTEGER,
  due_at          INTEGER NOT NULL,
  active          INTEGER NOT NULL DEFAULT 1,
  tokens_v2       TEXT    NOT NULL DEFAULT '',   -- jieba 预分词（v4 加）
  embedding       BLOB                           -- float32 packed（v5 加）
);
CREATE INDEX IF NOT EXISTS idx_facts_due ON facts(active, due_at);

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
  tsundere   REAL    NOT NULL,
  reliance   REAL    NOT NULL,
  energy     REAL    NOT NULL,
  captured_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_persona_snapshots_time ON persona_snapshots(captured_at DESC);
"""

SEED = """
INSERT OR IGNORE INTO persona (id, intimacy, tsundere, reliance, energy, updated_at)
VALUES (1, 12, 5, 20, 80, CAST(strftime('%s','now') AS INTEGER) * 1000);
"""
