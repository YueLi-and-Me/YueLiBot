"""管理进程级 SQLite 写连接，并将阻塞数据库操作移出事件循环。

模块使用单写连接配合 WAL，避免多个写连接互相等待；异步调用通过
``asyncio.to_thread`` 执行阻塞查询。连接生命周期由应用启动和关闭阶段控制，
建表及迁移委托给 ``migrations.manager``，确保已有数据库在备份和版本判断后再变更。
测试可传入 ``:memory:`` 创建隔离数据库。
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
    """打开或返回进程级 SQLite 写连接。

    DDL 和种子数据由迁移管理器在版本判断与备份之后执行，避免已有库提前应用目标
    结构导致迁移失败时无法恢复原始版本。

    :param path: SQLite 数据库文件路径，或 ``':memory:'``。

    :return: 进程级共享 SQLite 连接；如果已经打开连接，则忽略本次路径并返回已有连接。

    :raises sqlite3.Error: 数据库连接创建失败。

    副作用：
        首次调用创建连接、启用 ``sqlite3.Row`` 行工厂并保存模块级连接引用。
        连接禁用语句缓存（``cached_statements=0``）——这条连接会被线程池并发
        使用，缓存的 statement 被多线程复用会串结果，理由见下方注释。
    """
    global _db
    if _db is not None:
        return _db

    # [关键] cached_statements=0 不是性能取舍，是正确性要求。
    #
    # 这条连接被 run_in_thread 丢进线程池，多个请求会真正并发地用它。
    # - 现象：并发访问下 ``execute(...).fetchone()`` 会返回别的线程那一行、返回
    #   None，或抛 ``InterfaceError: bad parameter or other API misuse``。表现在
    #   界面上就是随机的 404/500——同一个键单独请求永远是好的，一批一起请求就有
    #   几条坏掉（实测 8 线程 × 60 次查询：72 次 None、67 次 InterfaceError）。
    # - 原因：不在 SQLite 层。``sqlite3.threadsafety == 3`` 说明 SQLite 编译在
    #   serialized 模式，跨线程共享连接本身是安全的。坏的是 Python 的语句缓存：
    #   ``execute`` 按 SQL 文本复用同一个 statement 对象，两个线程先后往同一个
    #   statement 绑参数再 step，绑定与结果集就串了。
    # - 后果：禁用缓存后同样的压测 480/480 全部正常。改回默认值等于把这类随机
    #   错误放回来，而它极难复现——单请求永远重现不了。
    #
    # 代价是每次调用重新 prepare 语句；本项目的查询都是小语句、量级也小，这点
    # 开销远小于「结果偶尔是错的」。
    db = sqlite3.connect(str(path), check_same_thread=False, cached_statements=0)
    db.row_factory = sqlite3.Row   # 让 fetchone/fetchall 返回 dict-like 对象
    _db = db
    return db


def get_db() -> sqlite3.Connection:
    """返回进程级 SQLite 连接。

    :return: 最近一次由 :func:`open_db` 打开的连接实例。
    :raises RuntimeError: 尚未调用 :func:`open_db`。
    副作用：不创建连接、不执行 SQL。
    """
    if _db is None:
        raise RuntimeError("数据库未初始化，请先调用 open_db()")
    return _db


async def run_in_thread(fn: Callable[..., _T], *args: Any) -> _T:
    """在线程池中执行阻塞的数据库调用，避免阻塞 asyncio 事件循环。

    :param fn: 要在线程池中调用的同步函数。
    :param *args: 传递给 ``fn`` 的位置参数。

    :return: ``fn(*args)`` 的结果，类型为 ``_T``。

    :raises Exception: ``fn`` 执行失败时传播其原始异常。

    副作用：
        占用事件循环默认线程池线程；不会自行创建或关闭数据库连接。
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)


def close_db() -> None:
    """关闭进程级 SQLite 连接并清空单例引用。

    :return: 无返回值；未打开连接时安全返回。
    副作用：关闭数据库连接，后续调用 :func:`get_db` 会失败，直到重新调用
        :func:`open_db`。
    :raises sqlite3.Error: 底层连接关闭失败时传播异常。
    """
    global _db
    if _db is not None:
        _db.close()
        _db = None
