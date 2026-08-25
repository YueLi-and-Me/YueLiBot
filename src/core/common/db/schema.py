"""定义主体后端使用的 SQLite 表结构、索引、全文索引和初始种子数据。

Python 侧直接使用 sqlite3 执行 DDL，不引入 ORM；无内容 FTS5 表的 rowid 与
业务表主键保持一致，记忆、人格、平台归属和观测事件由同一份 schema 管理。
"""

SCHEMA_VERSION = 3

EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_events (
  seq       INTEGER PRIMARY KEY AUTOINCREMENT,
  at        INTEGER NOT NULL,
  stream_id INTEGER,
  turn_id   INTEGER,
  stage     TEXT    NOT NULL DEFAULT '',
  kind      TEXT    NOT NULL,
  payload   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pipeline_events_at ON pipeline_events(at);
CREATE INDEX IF NOT EXISTS idx_pipeline_events_stream_seq ON pipeline_events(stream_id, seq);
CREATE INDEX IF NOT EXISTS idx_pipeline_events_turn ON pipeline_events(turn_id, seq);
"""

DDL = f"""
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

{EVENTS_DDL}

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
  sender_person_id INTEGER,
  -- 平台原生消息编号。出站引用回复必须把内部消息 ID 还原成平台编号才能发得出去，
  -- 桌面等无编号通道保持 NULL。
  external_message_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_pending ON messages(episode_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_stream_pending ON messages(stream_id, episode_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_stream_created ON messages(stream_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_stream_external
  ON messages(stream_id, external_message_id);

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

-- ------------------------------------------------------------ L3 知识与图谱
-- 以下六张表全部是纯新增，因此按既有惯例只写 CREATE TABLE IF NOT EXISTS、不写迁移：
-- 新增表不改动任何既有表结构，为它单开一个 user_version 只是多造一个要维护的版本号。
-- 建表时机与其余表一致，由启动时的幂等建表覆盖。
CREATE TABLE IF NOT EXISTS knowledge (
  id          INTEGER PRIMARY KEY,
  content     TEXT    NOT NULL,
  -- 去重键与 facts.content_key 同口径，迁移脚本重复执行时靠它保持幂等。
  content_key TEXT    NOT NULL UNIQUE,
  source      TEXT    NOT NULL DEFAULT '',
  tokens_v2   TEXT    NOT NULL DEFAULT '',
  -- float32 packed，与 facts.embedding 同格式。迁移进来的历史知识必须用当前向量
  -- 模型重算：不同模型的向量空间不可比，直接搬旧值检索结果是错的。
  embedding   BLOB,
  created_at  INTEGER NOT NULL,
  -- 命中计数与 facts.hit_count 同口径。本轮不参与检索打分，只落数据：
  -- 检索调优要的是「哪些知识真的被用到过」，而那份数据只能事后积累，
  -- 补列的窗口在真机建表之前，错过就得为两个整数付一次迁移。
  hit_count   INTEGER NOT NULL DEFAULT 0,
  last_hit_at INTEGER
);
-- 与 facts_fts 同款：content='' 的外部内容表，rowid 必须由写入方显式对齐主键，
-- 漏对齐会让检索结果指向错误的行。
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(tokens, content='', tokenize='unicode61');

CREATE TABLE IF NOT EXISTS knowledge_nodes (
  id         INTEGER PRIMARY KEY,
  concept    TEXT    NOT NULL UNIQUE,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_edges (
  id         INTEGER PRIMARY KEY,
  source_id  INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
  target_id  INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
  -- 关联强度＝共现次数，不是概率：迁移进来的历史边是 1~261 的整数计数，原样保留，
  -- 之后由使用频次继续累加。它只用于 related_concepts 的相对排序，相对序才是语义。
  -- ⚠ 消费方不得直接把它乘进复合打分（会被大计数支配），要乘先在使用处归一化。
  -- 需要 (0, 1] 语义的是 memory_edges.strength，那张表走 reinforce 的饱和口径。
  strength   REAL    NOT NULL DEFAULT 1.0,
  updated_at INTEGER NOT NULL,
  UNIQUE(source_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_edges_source ON knowledge_edges(source_id);

-- ---------------------------------------------------------------- 黑话与表达
CREATE TABLE IF NOT EXISTS jargon (
  id         INTEGER PRIMARY KEY,
  term       TEXT    NOT NULL,
  meaning    TEXT    NOT NULL,
  -- NULL 表示全局通用，非空表示只在该会话里成立。同一个词在不同群含义可以不同，
  -- 所以唯一键带上 stream_id。
  stream_id  INTEGER REFERENCES streams(id) ON DELETE CASCADE,
  -- confirmed 才参与提示词注入；pending 是尚未判定的候选，只存不用。
  status     TEXT    NOT NULL DEFAULT 'confirmed',
  hits       INTEGER NOT NULL DEFAULT 0,
  source     TEXT    NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  UNIQUE(term, stream_id)
);
CREATE INDEX IF NOT EXISTS idx_jargon_status ON jargon(status, stream_id);

CREATE TABLE IF NOT EXISTS expressions (
  id         INTEGER PRIMARY KEY,
  situation  TEXT    NOT NULL,
  style      TEXT    NOT NULL,
  stream_id  INTEGER REFERENCES streams(id) ON DELETE CASCADE,
  use_count  INTEGER NOT NULL DEFAULT 0,
  source     TEXT    NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  -- checked 预留给「只用人工确认过的表达」的质量闸门；当前全部置 0，读取不按它过滤。
  checked    INTEGER NOT NULL DEFAULT 0,
  -- 最近一次真正进提示词的时间，与 use_count 的回写同处发生；从未被选中为 NULL。
  last_used_at INTEGER,
  UNIQUE(situation, style, stream_id)
);

-- ------------------------------------------------------------ 记忆联想网络
-- 三层记忆各有各的生命周期与衰减口径，不能合并成一张宽表（那是三份重复真相的老错误），
-- 但联想需要一个统一的 id 空间来表达「这条事实和那段情节有关」。memory_nodes 是纯指针：
-- 删掉它不丢任何记忆，重建只要扫一遍 facts / episodes / knowledge 三张表。
CREATE TABLE IF NOT EXISTS memory_nodes (
  id       INTEGER PRIMARY KEY,
  ref_kind TEXT    NOT NULL,   -- 'fact' | 'episode' | 'knowledge'
  ref_id   INTEGER NOT NULL,
  UNIQUE(ref_kind, ref_id)
);
-- 边的唯一来源是「一起被点亮过」：同批写入，或同一次召回里真正进了提示词的那些。
-- 与 knowledge_edges 刻意分开——那张表表达「概念 A 与概念 B 相关」，这张表达
-- 「这两段记忆总是一起出现」，合并会让建边规则立刻分裂成两套。
-- 强度衰减复用 facts 的 retention 口径；跌破 FREEZE 只置非活跃，绝不删除。
CREATE TABLE IF NOT EXISTS memory_edges (
  id         INTEGER PRIMARY KEY,
  source_id  INTEGER NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
  target_id  INTEGER NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
  strength   REAL    NOT NULL DEFAULT 0.35,
  updated_at INTEGER NOT NULL,
  active     INTEGER NOT NULL DEFAULT 1,
  UNIQUE(source_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_edges_source ON memory_edges(active, source_id);

-- -------------------------------------------------------------- 人物画像缓存
-- 派生缓存，不是第三份真相：每一句都能追溯到某条 fact 或 episode，整表删掉重建
-- 不丢任何信息。dirty=1 表示有新证据待刷新，由后台任务批量重算，不在回合关键路径上。
CREATE TABLE IF NOT EXISTS person_profile (
  person_id      INTEGER PRIMARY KEY REFERENCES persons(id) ON DELETE CASCADE,
  summary        TEXT    NOT NULL DEFAULT '',
  evidence_count INTEGER NOT NULL DEFAULT 0,
  refreshed_at   INTEGER NOT NULL DEFAULT 0,
  dirty          INTEGER NOT NULL DEFAULT 1
);

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

-- ---------------------------------------------------------------- 表情包库
-- 文件保存在运行数据目录并以内容哈希命名；启动时按 send_ref 重算哈希。
CREATE TABLE IF NOT EXISTS emoji (
  hash          TEXT PRIMARY KEY,
  send_ref      TEXT    NOT NULL,
  emotion_tags  TEXT    NOT NULL,
  emotion_vec   BLOB,
  sub_type      INTEGER NOT NULL DEFAULT 1,
  seen_count    INTEGER NOT NULL DEFAULT 1,
  first_seen_at INTEGER NOT NULL
);

-- ---------------------------------------------------------------- 人格状态
-- 好感度按人保存；person_id=1 是 owner，其他 person 在首次交互时懒创建。
CREATE TABLE IF NOT EXISTS persona_bond (
  person_id  INTEGER PRIMARY KEY REFERENCES persons(id),
  intimacy   REAL    NOT NULL,
  updated_at INTEGER NOT NULL
);

-- energy 和 mood 是全局唯一的自身状态，使用 CHECK(id=1) 强制表中只保留一行。
CREATE TABLE IF NOT EXISTS persona_self (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  energy     REAL    NOT NULL,
  updated_at INTEGER NOT NULL,
  mood       REAL    NOT NULL DEFAULT 50.0
);

-- 旧人格表仅用于历史数据迁移，不再由当前运行时写入。
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
  captured_at INTEGER NOT NULL,
  mood       REAL    NOT NULL DEFAULT 50.0
);
CREATE INDEX IF NOT EXISTS idx_persona_snapshots_time ON persona_snapshots(captured_at DESC);

-- -------------------------------------------------------------- 生活活动时间线
-- 这里只记录实际发生或事后补叙的活动，不从旧日程推断历史事实。
CREATE TABLE IF NOT EXISTS activities (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  kind           TEXT    NOT NULL,
  doing          TEXT    NOT NULL,
  mood           TEXT    NOT NULL,
  energy_pace    INTEGER NOT NULL,
  mood_pace      INTEGER NOT NULL,
  advances       INTEGER,
  started_at     INTEGER NOT NULL,
  expected_until INTEGER NOT NULL,
  ended_at       INTEGER,
  source         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_time ON activities(started_at DESC);
"""

SEED = """
INSERT OR IGNORE INTO persons (id, kind, first_seen_at)
VALUES (1, 'owner', CAST(strftime('%s','now') AS INTEGER) * 1000);

INSERT OR IGNORE INTO streams (id, platform, kind, external_id)
VALUES (1, 'desktop', 'desktop', 'desktop');

INSERT OR IGNORE INTO persona_bond (person_id, intimacy, updated_at)
VALUES (1, 12, CAST(strftime('%s','now') AS INTEGER) * 1000);

INSERT OR IGNORE INTO persona_self (id, energy, mood, updated_at)
VALUES (1, 80, 50.0, CAST(strftime('%s','now') AS INTEGER) * 1000);

INSERT OR IGNORE INTO persona (id, intimacy, tsundere, reliance, energy, updated_at)
VALUES (1, 12, 5, 20, 80, CAST(strftime('%s','now') AS INTEGER) * 1000);
"""
