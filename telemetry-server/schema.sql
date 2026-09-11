-- 遥测服务端的全部持久状态。
--
-- installs 表里没有 IP 列，也没有任何可回溯到人的列——这是用户协议里
-- 「不采集 IP」的落地形式：不是承诺而是结构约束，没有那一列就无处可存。

CREATE TABLE IF NOT EXISTS installs (
  uuid           TEXT    PRIMARY KEY,
  first_seen     INTEGER NOT NULL,   -- 毫秒时间戳，注册时刻
  last_seen      INTEGER NOT NULL,   -- 毫秒时间戳，每次心跳更新
  app_version    TEXT    NOT NULL DEFAULT '',
  os_type        TEXT    NOT NULL DEFAULT '',
  python_version TEXT    NOT NULL DEFAULT ''
);

-- 「在线数」按 last_seen 过滤，装机量上万之后全表扫会慢。
CREATE INDEX IF NOT EXISTS idx_installs_last_seen ON installs(last_seen);

-- 每日快照。折线图要的是历史，而 installs 表只有 last_seen 一个时间点，
-- 天然画不出趋势；由 Cron 每日写一行补上这一维。
--
-- versions 只统计存活实例（last_seen 在 24 小时内），不是注册总数：
-- 装过一次再没开过的实例，其版本计数只增不减，图上会是一堆永不下降的线，
-- 而这张图要回答的恰恰是「谁还在用」。
CREATE TABLE IF NOT EXISTS daily_stats (
  day      TEXT    PRIMARY KEY,   -- UTC 日期，YYYY-MM-DD
  installs INTEGER NOT NULL,
  online   INTEGER NOT NULL,
  versions TEXT    NOT NULL       -- JSON：{"0.1.0": 812, ...}
);

-- 已发布的最新版本号，供 bot 的发布公告读取。
--
-- 只有一行，主键恒为 1：广播端点的语义是「现在最新是哪个版本」，保留历史版本
-- 只会让读取方多一次「哪一行才算数」的判断，而历史本来就在 git tag 里。
CREATE TABLE IF NOT EXISTS release_state (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  version      TEXT    NOT NULL,
  published_at INTEGER NOT NULL   -- 毫秒时间戳，写入时刻
);
