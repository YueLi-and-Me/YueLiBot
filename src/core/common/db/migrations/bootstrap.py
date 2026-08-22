"""在运行迁移链前对齐 SQLite 原生版本号和历史配置版本号。

历史实现把 schema 版本写在 `meta.schema_version`，没有维护 SQLite 原生的
`PRAGMA user_version`；当前迁移管理器则按原生版本号查找迁移函数。

因此已有数据库需要先从历史版本字段领取迁移入口；新数据库直接初始化为当前版本。

迁移前的三种情况分别是：没有 `messages` 表的新库直接标记当前版本；有业务表且
`user_version=0` 的旧库从 `meta.schema_version` 读取；已由当前迁移器接管的库保持原值。
"""

from __future__ import annotations

import sqlite3

from src.core.common.logger import get_logger

logger = get_logger(__name__)

# 历史运行时最后写入的 schema 版本。该值是迁移入口常量，不能随当前版本继续递增。
TS_FINAL_SCHEMA_VERSION = 3


def write_user_version(db: sqlite3.Connection, version: int) -> None:
    """把版本号写入 SQLite 原生 ``PRAGMA user_version``。

    :param db: 已打开的 SQLite 连接。
    :param version: 要写入的整数版本号，只允许已登记的迁移版本域。
    :return: 无返回值。
    :raises ValueError: 版本号不在已登记的迁移版本域内。
    :raises sqlite3.Error: PRAGMA 执行失败时传播数据库异常。
    副作用：修改数据库的 ``user_version`` 元信息；不提交事务。
    """
    # PRAGMA 赋值不支持参数绑定，动态构造语句文本也会被安全门禁判为注入路径；
    # 每个版本保留一条整句字面量，语句结构不可能被值改变，域外版本直接拒绝。
    # 新增迁移版本时必须同步补一条分支，漏补会在入口以 ValueError 暴露。
    if version == 1:
        db.execute("PRAGMA user_version = 1")
    elif version == 2:
        db.execute("PRAGMA user_version = 2")
    elif version == 3:
        db.execute("PRAGMA user_version = 3")
    elif version == 4:
        db.execute("PRAGMA user_version = 4")
    elif version == 5:
        db.execute("PRAGMA user_version = 5")
    elif version == 6:
        db.execute("PRAGMA user_version = 6")
    elif version == 7:
        db.execute("PRAGMA user_version = 7")
    elif version == 8:
        db.execute("PRAGMA user_version = 8")
    elif version == 9:
        db.execute("PRAGMA user_version = 9")
    elif version == 10:
        db.execute("PRAGMA user_version = 10")
    elif version == 11:
        db.execute("PRAGMA user_version = 11")
    elif version == 12:
        db.execute("PRAGMA user_version = 12")
    else:
        raise ValueError(f"未登记的 schema 版本号：{version}")


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    """判断指定名称的 SQLite 表是否存在。

    :param db: 已打开的 SQLite 连接。
    :param name: 要查询的表名。
    :return: 表存在时返回 `True`，否则返回 `False`。
    :raises sqlite3.Error: 元数据查询失败时传播数据库异常。
    副作用：只读 `sqlite_master`，不修改数据库。
    """
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _read_meta_schema_version(db: sqlite3.Connection) -> int | None:
    """读取历史 ``meta.schema_version`` 字段。

    :param db: 已打开的 SQLite 连接。

    :return: 可转换为整数的版本号；表、记录或字段值缺失/非法时返回 ``None``。

    :raises sqlite3.Error: 元数据查询失败时传播数据库异常。
    """
    if not _table_exists(db, "meta"):
        return None
    row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        # 非整数版本无法作为迁移入口，返回空值并让上层采用明确的历史默认值。
        logger.warning("meta_schema_version_unparsable", raw=repr(row[0]))
        return None


def is_fresh_database(db: sqlite3.Connection) -> bool:
    """判断数据库是否尚未创建业务消息表。

    使用 ``messages`` 而不是 ``meta`` 作为探针：两个表通常同时创建，但 ``meta``
    名称通用，其他模块单独创建它时不应阻止新库初始化。

    :param db: 已打开的 SQLite 连接。

    :return: ``messages`` 表不存在时返回 ``True``，否则返回 ``False``。

    :raises sqlite3.Error: 查询 SQLite 元数据失败。
    """
    return not _table_exists(db, "messages")


def bootstrap_version(db: sqlite3.Connection, current_version: int) -> int:
    """将 SQLite ``user_version`` 对齐到迁移链可识别的入口。

    该步骤只更新 ``user_version``，不修改业务表，保证重复执行幂等且无损。

    :param db: 已打开的 SQLite 连接。
    :param current_version: 当前应用支持的最新 schema 版本。

    :return: 对齐后的数据库版本号。

    :raises sqlite3.Error: 读取或写入 SQLite 版本元数据失败。

    副作用：
        可能写入 ``PRAGMA user_version`` 并记录迁移接管日志；不执行业务表 DDL/DML。
    """
    existing = db.execute("PRAGMA user_version").fetchone()[0]

    # 已存在原生版本号时，数据库已由当前迁移链接管，保持原值。
    if existing > 0:
        return existing

    # 全新安装：DDL 已经把表按最新形态建好了，没有历史需要迁移
    if is_fresh_database(db):
        write_user_version(db, current_version)
        logger.info("bootstrap_fresh_database", version=current_version)
        return current_version

    # 历史数据库从 meta 表恢复迁移入口。
    meta_version = _read_meta_schema_version(db)
    if meta_version is None:
        # 缺失版本时使用历史最终形态；低估会重复执行迁移，高估则会跳过不可逆迁移。
        meta_version = TS_FINAL_SCHEMA_VERSION
        logger.warning("bootstrap_meta_version_missing", assumed=meta_version)

    write_user_version(db, meta_version)
    logger.info(
        "bootstrap_adopted_ts_version",
        from_meta=meta_version,
        note="历史运行时只写入 meta.schema_version，未维护 user_version",
    )
    return meta_version
