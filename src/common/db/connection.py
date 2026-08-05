"""
SQLite 连接管理。

设计原则：
  · 单写连接：WAL 下多写连接会互相阻塞；单连接 + asyncio.to_thread 是标准做法
  · 阻塞查询一律用 asyncio.to_thread 包裹，防止事件循环在流式对话期间被卡住
  · 测试可传入 ':memory:' 拿到隔离的内存库

连接在进程生命周期内保持打开，不需要连接池。建表与迁移由 migrations.manager
统一编排，避免旧库在创建备份前就被最新 DDL 改写。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")

# 进程级单例
_db: sqlite3.Connection | None = None


def open_db(path: str | Path) -> sqlite3.Connection:
    """
    打开数据库并返回连接。

    DDL/SEED 必须由 migrations.manager 在版本与备份处理之后执行：已有库若在这里
    先执行目标 DDL，会使后续迁移失败时无法恢复为原始版本。
    """
    global _db
    if _db is not None:
        return _db

    db = sqlite3.connect(str(path), check_same_thread=False)
    db.row_factory = sqlite3.Row   # 让 fetchone/fetchall 返回 dict-like 对象
    _db = db
    return db


def get_db() -> sqlite3.Connection:
    """获取已打开的连接；须在 open_db() 之后调用。"""
    if _db is None:
        raise RuntimeError("数据库未初始化，请先调用 open_db()")
    return _db


async def run_in_thread(fn: Callable[..., _T], *args: Any) -> _T:
    """
    在线程池中执行阻塞的 sqlite3 调用，避免阻塞 asyncio 事件循环。

    用法：
        rows = await run_in_thread(db.execute, "SELECT ...", (param,)).fetchall()

    注意：sqlite3.Connection 本身不是线程安全的，但单写连接 + check_same_thread=False
    配合这里的 serialized 访问（asyncio 是单线程调度）是安全的。
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)


def close_db() -> None:
    """关闭连接（进程退出前调用）。"""
    global _db
    if _db is not None:
        _db.close()
        _db = None
