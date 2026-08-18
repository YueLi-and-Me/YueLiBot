"""
迁移管理器。

启动时读取 user_version，按迁移链逐步推进到最新版本。
每次迁移前自动备份原库文件。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import sqlite3

from .bootstrap import bootstrap_version, is_fresh_database
from .registry import get_registry

from src.core.common.db.schema import DDL, SEED
from src.core.common.logger import get_logger

logger = get_logger(__name__)

CURRENT_VERSION = 11  # 当前 schema 版本，表情包保留并回传 OneBot sub_type


def get_user_version(db: sqlite3.Connection) -> int:
    """读取 SQLite 原生 `PRAGMA user_version`。

    :param db: 已打开的 SQLite 连接。
    :return: 数据库版本号；PRAGMA 没有行时返回 `0`。
    :raises sqlite3.Error: 读取 PRAGMA 失败时传播数据库异常。
    副作用：只读数据库元信息。
    """
    row = db.execute("PRAGMA user_version").fetchone()
    return row[0] if row else 0


def set_user_version(db: sqlite3.Connection, version: int) -> None:
    """设置 SQLite 原生 schema 版本号。

    :param db: 已打开的 SQLite 连接。
    :param version: 要写入的整数版本号。
    :return: 无返回值。
    :raises sqlite3.Error: PRAGMA 执行失败时传播数据库异常。
    副作用：修改数据库的 `user_version` 元信息；不提交事务。
    """
    # PRAGMA user_version 不支持参数绑定，整数字面量是安全的
    db.execute(f"PRAGMA user_version = {version}")


def backup(db: sqlite3.Connection, db_path: Path) -> Path:
    """在数据库同目录的 `backups/` 下创建带版本和日期的 SQLite 快照。

    :param db: 当前数据库连接，用于调用 SQLite backup API。
    :param db_path: 原数据库文件路径。
    :return: 备份文件路径；同名备份已存在时直接返回该路径。
    :raises OSError: 备份目录或目标文件无法创建时抛出。
    :raises sqlite3.Error: SQLite 快照复制失败时抛出。
    副作用：创建备份目录和数据库文件，不修改源数据库。
    """
    backups_dir = db_path.parent / "backups"
    backups_dir.mkdir(exist_ok=True)
    version = get_user_version(db)
    stamp = datetime.now().strftime("%Y-%m-%d")
    dest = backups_dir / f"{db_path.stem}.v{version}.{stamp}{db_path.suffix}"
    if not dest.exists():
        backup_db = sqlite3.connect(str(dest))
        try:
            # sqlite 的 backup API 读取当前连接的一致性快照，WAL 中尚未 checkpoint 的页
            # 也会被带上；直接 copy 主 .db 文件做不到这一点。
            db.backup(backup_db)
        finally:
            backup_db.close()
        logger.info("db_backup_created", dest=str(dest))
    return dest


def _initialize_fresh_database(db: sqlite3.Connection) -> None:
    """使用当前 DDL 和种子数据初始化一个全新的数据库。

    :param db: 已打开且确认为空的 SQLite 连接。

    :raises sqlite3.Error: DDL、种子数据或版本号写入失败。

    副作用：
        创建当前 schema，写入初始数据，将 ``user_version`` 设为 ``CURRENT_VERSION``
        并提交事务。
    """
    db.executescript(DDL)
    db.executescript(SEED)
    set_user_version(db, CURRENT_VERSION)
    db.commit()
    logger.info("db_fresh_initialized", version=CURRENT_VERSION)


def _apply_current_schema(db: sqlite3.Connection) -> None:
    """幂等执行当前 DDL 和种子数据，补齐迁移后的最新结构。

    :param db: 已完成历史迁移的 SQLite 连接。

    :raises sqlite3.Error: 当前 DDL 或种子数据执行失败。

    副作用：
        可能创建缺失表、索引和种子记录，并提交当前事务。
    """
    db.executescript(DDL)
    db.executescript(SEED)
    db.commit()


def run_migrations(db: sqlite3.Connection, db_path: Path | None = None) -> None:
    """将数据库按注册迁移链推进到 ``CURRENT_VERSION``。

    :param db: 已打开的 SQLite 连接；可以是持久化数据库或内存数据库。
    :param db_path: 持久化数据库文件路径；传入 ``None`` 或 ``:memory:`` 时不创建迁移备份。

    :raises RuntimeError: 历史版本没有对应迁移函数，或迁移完整性检查失败。
    :raises OSError: 持久化数据库备份或文件操作失败。
    :raises sqlite3.Error: 迁移 SQL、事务提交或版本号写入失败。

    副作用：
        空库直接初始化；已有库在迁移前创建同目录备份，逐步更新 schema、数据和
        ``user_version``，任一步骤失败时回滚当前事务并传播异常。
    """
    # 空库必须先建为最新形态；已有库则绝不能在备份前执行目标 DDL/SEED。
    if is_fresh_database(db):
        _initialize_fresh_database(db)
        return

    # 先导入所有迁移模块，触发 @register 装饰器
    from . import (  # noqa: F401
        v3_to_v4,
        v4_to_v5,
        v5_to_v6,
        v6_to_v7,
        v7_to_v8,
        v8_to_v9,
        v9_to_v10,
        v10_to_v11,
    )

    registry = get_registry()

    # 先对齐历史版本字段与 user_version，再查找迁移注册表中的当前入口。
    current = bootstrap_version(db, CURRENT_VERSION)

    if current >= CURRENT_VERSION:
        logger.debug("db_up_to_date", version=current)
        _apply_current_schema(db)
        return

    logger.info("db_migration_start", from_version=current, to_version=CURRENT_VERSION)

    if db_path and db_path != Path(":memory:"):
        backup(db, db_path)

    while current < CURRENT_VERSION:
        fn = registry.get(current)
        if fn is None:
            raise RuntimeError(
                f"缺少从版本 {current} 到 {current + 1} 的迁移函数，"
                f"请在 migrations/ 下注册 @register({current})"
            )
        logger.info("db_migration_step", from_version=current, fn=fn.__name__)
        try:
            with db:  # 自动 commit/rollback
                fn(db)
                set_user_version(db, current + 1)
        except Exception as exc:
            logger.error("db_migration_failed", version=current, error=str(exc))
            raise
        current += 1

    _apply_current_schema(db)
    logger.info("db_migration_done", version=current)
