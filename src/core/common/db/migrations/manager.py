"""
迁移管理器。

启动时读取 user_version，按迁移链逐步推进到最新版本。
每次迁移前自动备份原库文件。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping

import sqlite3

from .bootstrap import (
    TS_FINAL_SCHEMA_VERSION,
    bootstrap_version,
    is_fresh_database,
    write_user_version,
)
from .registry import MigrationFn, get_registry

from src.core.common.db.schema import DDL, SEED
from src.core.common.db.schema_report import (
    describe_schema_changes,
    report_schema_changes,
    snapshot_shape,
)
from src.core.common.logger import get_logger

logger = get_logger(__name__)

CURRENT_VERSION = 28  # 当前 schema 版本：导入中心来源批次、画像信任分级、stream 群聊展示名、事实操作流水


def load_migration_registry() -> Dict[int, MigrationFn]:
    """导入全部迁移模块并返回注册表快照。

    迁移执行与运行时自检必须从同一个入口装载注册表；否则新迁移只在其中一边登记，
    自检会给出错误的健康结论。

    :return: 起始版本到迁移函数的注册表副本。
    :raises ImportError: 任一迁移模块缺失或无法导入。
    副作用：首次调用时导入迁移模块，触发各模块的 ``@register`` 装饰器。
    """
    from . import (  # noqa: F401
        v3_to_v4,
        v4_to_v5,
        v5_to_v6,
        v6_to_v7,
        v7_to_v8,
        v8_to_v9,
        v9_to_v10,
        v10_to_v11,
        v11_to_v12,
        v12_to_v13,
        v13_to_v14,
        v14_to_v15,
        v15_to_v16,
        v16_to_v17,
        v17_to_v18,
        v18_to_v19,
        v19_to_v20,
        v20_to_v21,
        v21_to_v22,
        v22_to_v23,
        v23_to_v24,
        v24_to_v25,
        v25_to_v26,
        v26_to_v27,
        v27_to_v28,
    )

    return get_registry()


def migration_chain_errors(
    registry: Mapping[int, MigrationFn],
    current_version: int = CURRENT_VERSION,
) -> List[str]:
    """返回迁移注册表相对当前版本的结构错误。

    :param registry: 起始版本到迁移函数的只读映射。
    :param current_version: 代码声明的最新数据库版本。
    :return: 逐条可定位的错误；空列表表示从历史入口到当前版本连续且无越界项。
    副作用：无。
    """
    expected = set(range(TS_FINAL_SCHEMA_VERSION, current_version))
    actual = set(registry)
    errors: List[str] = []
    for version in sorted(expected - actual):
        errors.append(
            f'缺少从版本 {version} 到 {version + 1} 的迁移函数，'
            f'请在 migrations/ 下注册 @register({version})'
        )
    for version in sorted(actual - expected):
        errors.append(
            f'迁移注册号 {version} 超出版本域 '
            f'v{TS_FINAL_SCHEMA_VERSION}→v{current_version}'
        )
    if actual:
        chain_head = max(actual) + 1
        if chain_head != current_version:
            errors.append(
                f'迁移链头是 v{chain_head}，CURRENT_VERSION 是 v{current_version}'
            )
    else:
        errors.append(
            f'迁移注册表为空，无法从 v{TS_FINAL_SCHEMA_VERSION} 推进到 v{current_version}'
        )
    return errors


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
    # 语句构造与版本域约束统一收敛在 write_user_version，见该函数说明。
    write_user_version(db, version)


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
    report_schema_changes(
        describe_schema_changes({}, snapshot_shape(db)),
        CURRENT_VERSION,
        db,
    )


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

    :raises RuntimeError: 历史版本没有对应迁移函数。
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
    registry = load_migration_registry()
    # 整轮对账：迁移的 ALTER TABLE 与 DDL 的建表都要落进同一份报告，
    # 否则「这次启动到底改了库的什么」要分两处看。
    before = snapshot_shape(db)

    # 先对齐历史版本字段与 user_version，再查找迁移注册表中的当前入口。
    current = bootstrap_version(db, CURRENT_VERSION)

    if current >= CURRENT_VERSION:
        logger.debug("db_up_to_date", version=current)
        _apply_current_schema(db)
        report_schema_changes(
            describe_schema_changes(before, snapshot_shape(db)),
            current,
            db,
        )
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
    report_schema_changes(
        describe_schema_changes(before, snapshot_shape(db)),
        current,
        db,
    )
    logger.info("db_migration_done", version=current)
