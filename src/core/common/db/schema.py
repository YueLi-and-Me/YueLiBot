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

-- 检索调优的参数快照。新表无存量数据回填需求，按 jargon 先例由链尾 DDL
-- 幂等建表，不占迁移号；生效名存 meta 表的 retrieval_tuning_active_profile。
CREATE TABLE IF NOT EXISTS retrieval_profiles (
  name             TEXT PRIMARY KEY,
  params           TEXT    NOT NULL,
  created_at       INTEGER NOT NULL,
  last_applied_at  INTEGER
);

-- ---------------------------------------------------------------- stream / person 归属
CREATE TABLE IF NOT EXISTS persons (
  id            INTEGER PRIMARY KEY,
  kind          TEXT    NOT NULL,
  first_seen_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS streams (
  id           INTEGER PRIMARY KEY,
  platform     TEXT    NOT NULL,
  kind         TEXT    NOT NULL,
  external_id  TEXT    NOT NULL,
  -- 平台侧可读的会话名称；当前用于群名，NULL 表示尚未拉到并退回 external_id。
  display_name TEXT,
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
  stream_id  INTEGER NOT NULL DEFAULT 1,
  -- 纠错命中后置 1，等待后台重摘要；待重建期间可选择屏蔽召回（v24 加）。
  needs_rebuild INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_episodes_time ON episodes(ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_stream_time ON episodes(stream_id, ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_rebuild ON episodes(needs_rebuild);

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
  -- 自解释 SQ8：头部携带格式版本、维度和单向量 scale；原 float32 列继续保留。
  embedding_q8    BLOB,
  -- 事实被听见的场合（v21 加）：group 群里听到 / direct 私聊或桌面听到 /
  -- legacy 迁移前的存量行。可见范围由它决定，见 memory/scope.py。
  origin_kind     TEXT    NOT NULL DEFAULT 'legacy',
  -- 事实账本（v22 加）：slot 是单值槽位名，多值事实留空；
  -- superseded_by 非空即已被取代失效，同时记录取代链指向谁。
  slot            TEXT    NOT NULL DEFAULT '',
  superseded_by   INTEGER REFERENCES facts(id),
  UNIQUE(person_id, content_key)
);
CREATE INDEX IF NOT EXISTS idx_facts_due ON facts(active, due_at);
CREATE INDEX IF NOT EXISTS idx_facts_person_active ON facts(person_id, active);
-- 冲突检测按（人, 槽位）成组查询。（v22 加）
CREATE INDEX IF NOT EXISTS idx_facts_person_slot ON facts(person_id, slot);

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
  -- 自解释 SQ8，与原向量并存；读取方可逐条校验格式和维度。
  embedding_q8 BLOB,
  created_at  INTEGER NOT NULL,
  -- 命中计数与 facts.hit_count 同口径。本轮不参与检索打分，只落数据：
  -- 检索调优要的是「哪些知识真的被用到过」，而那份数据只能事后积累，
  -- 补列的窗口在真机建表之前，错过就得为两个整数付一次迁移。
  hit_count   INTEGER NOT NULL DEFAULT 0,
  last_hit_at INTEGER,
  -- 指向导入来源批次；NULL 表示无批次（存量迁移与运行期抽取），
  -- 按批次删除的 WHERE 子句不含 NULL 行，撤销永远碰不到它们。
  import_batch_id INTEGER REFERENCES import_batches(id) ON DELETE CASCADE
);

-- 导入中心的来源批次：撤销的最小单位。「这批资料过时了」必须能整批撤掉，
-- 否则导入是单向操作；批次记住谁导的、什么时候、原始名与统计。
CREATE TABLE IF NOT EXISTS import_batches (
  id           INTEGER PRIMARY KEY,
  kind         TEXT    NOT NULL,
  origin_name  TEXT    NOT NULL DEFAULT '',
  summary      TEXT    NOT NULL DEFAULT '',
  submitted    INTEGER NOT NULL DEFAULT 0,
  added        INTEGER NOT NULL DEFAULT 0,
  status       TEXT    NOT NULL DEFAULT 'running',
  created_at   INTEGER NOT NULL,
  finished_at  INTEGER,
  error        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_import_batches_created ON import_batches(created_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_import_batch ON knowledge(import_batch_id);
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
  -- confirmed 才参与提示词注入；pending 是尚未判定的候选，只存不用；rejected 是
  -- 人工驳回，同样不注入，且写入时会把 inferred_at_sightings 顶到锁定档，避免
  -- 自动推断把人的结论改回来。「判定为普通词」不设新取值：pending 且
  -- inferred_at_sightings > 0 即是。
  status     TEXT    NOT NULL DEFAULT 'confirmed',
  hits       INTEGER NOT NULL DEFAULT 0,
  source     TEXT    NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  -- 学习期证据三列（v18）。sightings 是「本批语料出现过」的累计，每批每词至多
  -- +1；它与 hits（查表命中，含被截掉没进提示词的）语义不同，不能混用。
  -- evidence_ids 存证据消息 id 的 JSON 数组，推断时据此取上下文；
  -- inferred_at_sightings 记上次推断时的 sightings，到 100 视为锁定不再推断。
  sightings             INTEGER NOT NULL DEFAULT 0,
  evidence_ids          TEXT,
  inferred_at_sightings INTEGER NOT NULL DEFAULT 0,
  UNIQUE(term, stream_id)
);
CREATE INDEX IF NOT EXISTS idx_jargon_status ON jargon(status, stream_id);

-- 会话高频词表：后台任务按会话统计 messages 得出的真实高频词，供黑话召回打分
-- （命中的词条拿到碾压性加分，未命中的误报靠它沉底）。它是统计快照不是词典——
-- 整表按 built_at 全量重写，不逐行累积，因此没有 created_at/updated_at 双口径。
CREATE TABLE IF NOT EXISTS high_frequency_terms (
  stream_id        INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
  term             TEXT    NOT NULL,
  -- 出现总次数与出现过它的消息条数：前者量强度，后者量覆盖面，排序先次数后条数。
  occurrence_count INTEGER NOT NULL,
  message_count    INTEGER NOT NULL,
  -- 会话内的名次（1 起），打分公式里 max(0, 100 - rank) 直接使用。
  rank             INTEGER NOT NULL,
  built_at         INTEGER NOT NULL,
  PRIMARY KEY (stream_id, term)
);

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
-- 派生缓存，不是第三份真相：确凿档逐条对应某条 fact，印象档能追溯到本地证据，
-- 整表删掉重建不丢任何信息。dirty=1 表示有新证据待刷新，由后台任务批量重算，
-- 不在回合关键路径上。
CREATE TABLE IF NOT EXISTS person_profile (
  person_id      INTEGER PRIMARY KEY REFERENCES persons(id) ON DELETE CASCADE,
  -- v26 起语义收窄为「印象」档：只存模型收敛出的理解。此前它是唯一的画像正文，
  -- 存量行不重算，整段视作印象保留（不给已有数据编造出处），确凿档待自然刷新填上。
  summary        TEXT    NOT NULL DEFAULT '',
  -- 确凿档（v26 加）：facts 账本直接投影的 JSON 数组，每条带 fact_id 可回溯；
  -- 只有账本投影能写入，模型产出永远进不了这一列。
  confirmed      TEXT    NOT NULL DEFAULT '',
  -- 参与上一轮生成的 fact / episode id 的稳定哈希（v26 加）：置脏后指纹没变就只
  -- 推进 refreshed_at、清脏位，不再调用模型；空串表示还没按指纹口径刷新过。
  evidence_fingerprint TEXT NOT NULL DEFAULT '',
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
-- use_count / last_used_at 记录「她自己发过几次」，与 seen_count（入站又见到）
-- 分开：淘汰判据只读前者。
CREATE TABLE IF NOT EXISTS emoji (
  hash          TEXT PRIMARY KEY,
  send_ref      TEXT    NOT NULL,
  emotion_tags  TEXT    NOT NULL,
  emotion_vec   BLOB,
  sub_type      INTEGER NOT NULL DEFAULT 1,
  seen_count    INTEGER NOT NULL DEFAULT 1,
  use_count     INTEGER NOT NULL DEFAULT 0,
  last_used_at  INTEGER,
  first_seen_at INTEGER NOT NULL
);

-- 封禁按内容哈希独立存在，不以 emoji 行为宿主：行被淘汰或文件被删之后
-- 封禁必须仍然生效，同一张图不能因为删了一次就又进得来。
CREATE TABLE IF NOT EXISTS emoji_banned (
  hash      TEXT PRIMARY KEY,
  banned_at INTEGER NOT NULL,
  reason    TEXT
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

-- -------------------------------------------------------------- N4 反馈纠错（v24 加）
-- 待观察项：一条事实真的进了提示词时登记一行，窗口内等待用户纠正信号。
CREATE TABLE IF NOT EXISTS memory_feedback_pending (
  id         INTEGER PRIMARY KEY,
  fact_id    INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  person_id  INTEGER NOT NULL,
  stream_id  INTEGER NOT NULL,
  entered_at INTEGER NOT NULL,
  status     TEXT    NOT NULL DEFAULT 'pending',
  attempts   INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  UNIQUE(fact_id, stream_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_pending_status ON memory_feedback_pending(status, entered_at);

-- 纠错结果：判定成立的纠正留档；marked=1 表示该事实带「已被纠正」标记。
CREATE TABLE IF NOT EXISTS memory_feedback_results (
  id                INTEGER PRIMARY KEY,
  fact_id           INTEGER NOT NULL,
  person_id         INTEGER NOT NULL,
  stream_id         INTEGER NOT NULL,
  confidence        REAL    NOT NULL,
  corrected_content TEXT    NOT NULL DEFAULT '',
  new_fact_id       INTEGER NOT NULL DEFAULT 0,
  marked            INTEGER NOT NULL DEFAULT 0,
  created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_results_fact ON memory_feedback_results(fact_id, marked);

-- ---------------------------------------------------------- 事实操作流水（v28 加）
-- 人工管理与自动取代共用的事实账本流水：每行记录一次对 facts 行的状态改写，
-- prev 留操作前的值作为撤销依据，undone_by/undo_of 构成撤销链。流水只增不改，
-- 是审计与撤销的唯一依据；失效判据仍是 facts.superseded_by，本表不参与召回。
CREATE TABLE IF NOT EXISTS fact_operations (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  at              INTEGER NOT NULL,               -- Unix 毫秒
  actor           TEXT    NOT NULL,               -- 'manual' | 'n4' | 'auto'
  op              TEXT    NOT NULL,               -- invalidate/restore/pin/unpin/supersede/replace/adjudicate/undo
  person_id       INTEGER NOT NULL,
  fact_id         INTEGER NOT NULL,               -- 被操作的行
  related_fact_id INTEGER,                        -- supersede/replace 的新行；adjudicate 保留的行
  prev            TEXT    NOT NULL DEFAULT '{{}}',  -- JSON：操作前的值（撤销依据）
  undone_by       INTEGER REFERENCES fact_operations(id),  -- 本条被哪条撤销
  undo_of         INTEGER REFERENCES fact_operations(id)   -- 本条是撤销了谁
);
CREATE INDEX IF NOT EXISTS idx_fact_operations_fact ON fact_operations(fact_id);
CREATE INDEX IF NOT EXISTS idx_fact_operations_person ON fact_operations(person_id, at);
-- ---------------------------------------------------------- 运行画像（v29 加）
-- 一台机器自己的运行画像：随机安装 ID、应用版本、启动次数、累计运行时长。
-- 这是可丢的派生数据，不是第二份真相：整表删掉只丢历史统计，不影响任何功能，
-- 下次启动由幂等建表重建并从零重新采集（安装 ID 也随之重新生成，它只存在这里）。
-- 安装 ID 是首次启动生成的随机值，与 QQ 号、机器名、MAC、路径等任何可关联到
-- 人的标识无派生关系；本表不记路径、不记网络信息、不记配置内容。
CREATE TABLE IF NOT EXISTS runtime_profile (
  id               INTEGER PRIMARY KEY CHECK (id = 1),
  -- 随机安装 ID（secrets 生成）；空串表示尚未生成。
  install_id       TEXT    NOT NULL DEFAULT '',
  -- 最近启动时的应用版本，来自 D2 的版本单一来源；来源未落地时为空串。
  app_version      TEXT    NOT NULL DEFAULT '',
  launch_count     INTEGER NOT NULL DEFAULT 0,
  first_launch_at  INTEGER,
  last_launch_at   INTEGER,
  -- 已累计落库的运行时长（毫秒）。按心跳周期追加，进程被强杀时损失不超一个间隔。
  total_runtime_ms INTEGER NOT NULL DEFAULT 0,
  updated_at       INTEGER
);
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
