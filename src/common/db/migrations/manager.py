"""
迁移管理器。

启动时读取 user_version，按迁移链逐步推进到最新版本。
每次迁移前自动备份原库文件。
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from yueli.common.logger import get_logger
from .registry import get_registry

logger = get_logger(__name__)

CURRENT_VERSION = 5  # v5 adds embedding BLOB column to facts


def get_user_version(db: sqlite3.Connection) -> int:
    row = db.execute("PRAGMA user_version").fetchone()
    return row[0] if row else 0


def set_user_version(db: sqlite3.Connection, version: int) -> None:
    # PRAGMA user_version 不支持参数绑定，整数字面量是安全的
    db.execute(f"PRAGMA user_version = {version}")


def backup(db_path: Path) -> Path:
    """在同目录 backups/ 子目录下创建带版本号和日期的备份。"""
    backups_dir = db_path.parent / "backups"
    backups_dir.mkdir(exist_ok=True)
    version = get_user_version(sqlite3.connect(str(db_path)))
    stamp = datetime.now().strftime("%Y-%m-%d")
    dest = backups_dir / f"{db_path.stem}.v{version}.{stamp}{db_path.suffix}"
    if not dest.exists():
        shutil.copy2(db_path, dest)
        logger.info("db_backup_created", dest=str(dest))
    return dest


def run_migrations(db: sqlite3.Connection, db_path: Path | None = None) -> None:
    """
    将数据库从当前版本迁移到 CURRENT_VERSION。

    db_path 非 None 时，迁移前先备份（:memory: 测试库不备份）。
    """
    # 先导入所有迁移模块，触发 @register 装饰器
    from . import v3_to_v4, v4_to_v5  # noqa: F401
    from .bootstrap import bootstrap_version

    registry = get_registry()

    # ★ 必须先对齐版本号入口再读 current。
    #   TS 侧只写 meta.schema_version，没维护 user_version，
    #   直接读 user_version 会拿到 0，然后在链上找不到 @register(0) 而崩。
    #   见 bootstrap.py 顶部说明。
    current = bootstrap_version(db, CURRENT_VERSION)

    if current >= CURRENT_VERSION:
        logger.debug("db_up_to_date", version=current)
        return

    logger.info("db_migration_start", from_version=current, to_version=CURRENT_VERSION)

    if db_path and db_path != Path(":memory:"):
        backup(db_path)

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

    logger.info("db_migration_done", version=current)
