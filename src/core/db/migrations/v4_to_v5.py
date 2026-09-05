"""
v4 → v5 迁移：为 facts 表加 embedding 列，供向量混合召回使用。

embedding 存原始 float32 packed bytes；NULL 表示尚未计算。
Python 启动时如果 features.toml 里 [vector].enabled = true 且存在 NULL 行，
会在后台异步补算，不阻塞启动。
"""

from __future__ import annotations

import sqlite3

from src.core.logging.logger import get_logger
from .registry import register

logger = get_logger(__name__)


@register(4)
def v4_to_v5(db: sqlite3.Connection) -> None:
    """为 ``facts`` 表添加可为空的 embedding BLOB 列。

    :param db: 当前迁移事务使用的 SQLite 连接。

    :raises sqlite3.Error: 表结构查询或列添加失败。

    副作用：
        在 ``facts`` 缺少 ``embedding`` 列时添加该列；已有列时保持数据不变，
        不提交事务。
    """
    cols = {row[1] for row in db.execute("PRAGMA table_info(facts)").fetchall()}
    if "embedding" not in cols:
        db.execute("ALTER TABLE facts ADD COLUMN embedding BLOB")
    logger.info("v4_to_v5_done", note="embedding 列已就绪，NULL 行等待后台异步填充")
