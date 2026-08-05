"""
迁移链的入口对齐（bootstrap）。

★ 这个模块解决的是一个**只在真实库上才出现**的问题。

TS 侧把 schema 版本写在 `meta` 表里（`schema_version = '3'`），
从来没碰过 SQLite 原生的 `PRAGMA user_version` —— 它一直是 0。
而 Python 侧的迁移管理器按 `user_version` 找迁移链。

结果：任何一个真实的 memory.db 都会让后端在启动时崩在
「缺少从版本 0 到 1 的迁移函数」上，因为链上只注册了 @register(3)。

纯 `:memory:` 单测永远命中不到这一条 —— 空库里两个版本号都是 0，
天然一致。这正是「单测全绿但启动就炸」的典型形态。

所以在跑迁移链之前，必须先把两套版本号对齐：
  · 空库（没有 messages 表）        → 全新安装，DDL 建表后直接标成当前版本
  · 有表但 user_version = 0        → TS 时代的库，从 meta.schema_version 领取版本
  · user_version > 0               → 已经由 Python 接管过，不动
"""

from __future__ import annotations

import sqlite3

from src.common.logger import get_logger

logger = get_logger(__name__)

# TS 侧 schema.ts 里的 SCHEMA_VERSION。写死是刻意的：
# 它标记的是「移交给 Python 之前，TS 最后留下的形态」，是一个历史常量，
# 不该随 Python 侧 CURRENT_VERSION 一起往上走。
TS_FINAL_SCHEMA_VERSION = 3


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _read_meta_schema_version(db: sqlite3.Connection) -> int | None:
    """读 TS 侧写在 meta 表里的版本号。读不到或不是整数就返回 None。"""
    if not _table_exists(db, "meta"):
        return None
    row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        # meta 里存了非整数，说明这库被别的东西写过，不猜
        logger.warning("meta_schema_version_unparsable", raw=repr(row[0]))
        return None


def is_fresh_database(db: sqlite3.Connection) -> bool:
    """
    空库判定。

    用 `messages` 而不是 `meta` 作为探针：MemoryStore 建表时两者一起建，
    但 `meta` 这个名字太通用，将来若有别的模块也用它，会误判成「已有数据」。
    """
    return not _table_exists(db, "messages")


def bootstrap_version(db: sqlite3.Connection, current_version: int) -> int:
    """
    把 user_version 对齐到迁移链能识别的入口，返回对齐后的版本号。

    只写 user_version，绝不碰任何业务表 —— 这一步必须是幂等的、无损的。
    """
    existing = db.execute("PRAGMA user_version").fetchone()[0]

    # 已经被 Python 管过，链条自洽，不要插手
    if existing > 0:
        return existing

    # 全新安装：DDL 已经把表按最新形态建好了，没有历史需要迁移
    if is_fresh_database(db):
        db.execute(f"PRAGMA user_version = {current_version}")
        logger.info("bootstrap_fresh_database", version=current_version)
        return current_version

    # TS 时代的库：从 meta 领取版本号
    meta_version = _read_meta_schema_version(db)
    if meta_version is None:
        # 有表但 meta 里没版本号。这种库只可能来自很早的构建，
        # 按 TS 的最终形态处理是唯一安全的假设 —— 猜低了会重复跑迁移，
        # 猜高了会跳过必要的迁移，而后者是不可逆的
        meta_version = TS_FINAL_SCHEMA_VERSION
        logger.warning("bootstrap_meta_version_missing", assumed=meta_version)

    db.execute(f"PRAGMA user_version = {meta_version}")
    logger.info(
        "bootstrap_adopted_ts_version",
        from_meta=meta_version,
        note="TS 侧只写 meta.schema_version，未维护 user_version",
    )
    return meta_version
