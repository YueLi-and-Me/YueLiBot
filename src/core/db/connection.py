"""管理进程级 SQLite 数据库句柄，并将阻塞数据库操作移出事件循环。

对外暴露的 :class:`Database` 在接口上与 ``sqlite3.Connection`` 等价，内部按线程
各自持有一条真实连接：本进程会把数据库调用同时交给事件循环线程和
``run_in_thread`` 的线程池，而 SQLite 的事务状态属于连接不属于线程，共用一条连接
会让两个线程的事务互相渗透，理由见 :class:`Database` 的类注释。

连接生命周期由应用启动和关闭阶段控制（``main`` 调 :func:`open_db` 与
:func:`close_db`），建表及迁移委托给 ``migrations.manager``，确保已有数据库在备份和
版本判断之后再变更。测试传入 ``':memory:'`` 可得到一个进程内隔离、线程间共享的
内存库。

注解口径：全仓的数据库参数仍标注为 ``sqlite3.Connection``。:class:`Database` 在接口
上与它等价，改注解是一次波及两百余处、跨几十个文件的机械变更，与正在进行的重构会
正面冲突，因此单独处理；在那之前这些注解按「数据库句柄」理解。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

_T = TypeVar("_T")

# 一条语句等待另一线程结束写事务的上限，单位毫秒。WAL 下读者不被写者阻塞，但写者之间
# 仍然互斥；超时抛 sqlite3.OperationalError，不做静默重试。取值需覆盖本项目最长的单次
# 写事务（迁移与批量删除在毫秒量级），5000 留足两个数量级余量。数值与 sqlite3 模块的
# 默认值（``connect(timeout=5.0)``）相同，仍显式写出：单连接时代不存在自己等自己，这个
# 值从按线程分连接起才真正决定跨线程写入的成败，不该继续搭在库的默认值上。
BUSY_TIMEOUT_MS = 5000

# 进程级单例
_db: Database | None = None

# 为每个内存库句柄生成互不相同的库名，见 _resolve_dsn。
_memory_sequence = 0


def _resolve_dsn(path: str | Path) -> tuple[str, bool]:
    """把调用方给的路径翻译成真正用于连接的 DSN。

    :param path: 数据库文件路径，或 ``':memory:'``。
    :return: ``(dsn, 是否按 URI 解析)`` 二元组。

    ``':memory:'`` 必须翻译成共享缓存的 URI：普通内存库属于单条连接，按线程建连接
    之后每个线程会各自拿到一个空库；共享缓存让同名内存库在进程内只有一份。库名带
    递增序号，保证两次 :func:`open_db` 之间互不串数据（用例逐个函数重开库）。
    """

    text = str(path)
    if text != ':memory:':
        return text, False
    global _memory_sequence
    _memory_sequence += 1
    return f'file:yueli-memory-{_memory_sequence}?mode=memory&cache=shared', True


class Database:
    """进程级数据库句柄：对外是一个连接对象，对内每个线程各持一条独立连接。

    [关键] 按线程建连接不是性能取舍，是正确性要求。

    - 现象：一个写入单元的两条 UPDATE 之间，线程池里任何一次 ``commit()`` 都会把这
      半截状态提交掉；写入单元中途失败时事务悬在连接上，别人的下一次提交替它落盘，
      它自己的 ``rollback()`` 只能撤回剩下的一半。
    - 原因：SQLite 的事务状态属于连接。旧实现把一条 ``check_same_thread=False`` 的
      连接交给 ``run_in_thread`` 的线程池真正并发使用，两个线程共用同一个事务。
    - 后果：静默、不可逆、无日志——只有并发窗口恰好命中时发生，事后在日志里找不到
      痕迹。改回共用一条连接等于把这类数据损坏放回来。

    随之而来、必须知道的三条约束：

    - ``PRAGMA foreign_keys`` 与 ``busy_timeout`` 是连接级设置，每条新连接都要重设，
      见 :meth:`_connect`。
    - 写者之间在 WAL 下仍互斥，跨线程并发写会等待 ``BUSY_TIMEOUT_MS`` 后抛错，而不是
      像单连接时代那样天然排队。
    - 未提交的写入不再跨线程可见。依赖「别人的提交替自己落盘」的写入路径会失去这个
      偶然效果——那是要暴露的缺陷，不是要保留的行为。
    - 连接按线程累积且不随线程结束回收，直到 :meth:`close`。事件循环的默认线程池容量
      有上限（``min(32, cpu + 4)``），因此连接数也有上限；若将来换成无上限的线程来源，
      这里要改成随线程退出回收。

    未在本类显式列出的成员一律经 :meth:`__getattr__` 转发到**当前线程**的连接：漏列
    一个成员就等于让它落回错误的连接上，而这种遗漏是静默的，因此这里用属性转发而不
    是逐个包装。
    """

    def __init__(self, path: str | Path) -> None:
        """打开数据库句柄并建立创建者线程的连接。

        :param path: SQLite 数据库文件路径，或 ``':memory:'``。
        :raises sqlite3.Error: 首条连接创建或 PRAGMA 设置失败。
        副作用：立即创建一条真实连接。内存库随最后一条连接关闭而消失，这条连接同时
            充当共享缓存内存库的锚，必须在句柄存活期间一直开着。
        """

        self._dsn, self._uri = _resolve_dsn(path)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._settings: dict[str, Any] = {}
        self._trace: Callable[[str], None] | None = None
        self._guard = threading.Lock()
        self._anchor = self._for_thread()

    # ---- 连接管理 ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """为当前线程建立一条新连接并补齐所有连接级设置。"""

        connection = sqlite3.connect(
            self._dsn,
            uri=self._uri,
            # 写者之间的等待上限，见 BUSY_TIMEOUT_MS。
            timeout=BUSY_TIMEOUT_MS / 1000,
            # 关闭时由持有句柄的线程统一关掉别的线程的连接，所以不能开同线程检查。
            check_same_thread=False,
            # 语句缓存曾经串结果：并发共用一条连接时，两个线程先后往同一个 statement
            # 绑参数再 step，绑定与结果集互串，实测 8 线程 x 60 次查询出现 72 次 None
            # 与 67 次 InterfaceError。该竞态的根因与事务渗透同源，已被按线程建连接
            # 消除；这里仍显式置 0，代价是每次重新 prepare 小语句，换取连接一旦再被
            # 跨线程共用时不会退化成随机错误。
            cached_statements=0,
        )
        connection.row_factory = sqlite3.Row   # 让 fetchone/fetchall 返回 dict-like 对象
        # 外键约束的开关属于连接而不属于数据库文件：schema 的 DDL 里那条 PRAGMA 只对
        # 执行它的那条连接有效，新建的连接默认是 OFF，漏设会让该线程上的写入绕过全部
        # 外键约束且不报错。
        connection.execute('PRAGMA foreign_keys = ON')
        for name, value in self._settings.items():
            setattr(connection, name, value)
        if self._trace is not None:
            connection.set_trace_callback(self._trace)
        return connection

    def _for_thread(self) -> sqlite3.Connection:
        """返回当前线程的连接，必要时建立。

        :return: 归当前线程独占的 ``sqlite3.Connection``。
        :raises sqlite3.Error: 新连接创建失败。
        """

        try:
            return self._local.connection
        except AttributeError:
            pass
        connection = self._connect()
        self._local.connection = connection
        with self._guard:
            self._connections.append(connection)
        return connection

    @property
    def thread_connection_count(self) -> int:
        """当前句柄下已建立的真实连接数；供观测与用例断言使用。"""

        with self._guard:
            return len(self._connections)

    # ---- sqlite3.Connection 接口 ---------------------------------------

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        """在当前线程的连接上执行一条语句。"""

        return self._for_thread().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any) -> sqlite3.Cursor:
        """在当前线程的连接上对一批参数执行同一条语句。"""

        return self._for_thread().executemany(sql, parameters)

    def executescript(self, script: str) -> sqlite3.Cursor:
        """在当前线程的连接上执行多语句脚本。

        注意 sqlite3 会在执行脚本之前隐式提交当前事务，这是库的既有行为。
        """

        return self._for_thread().executescript(script)

    def commit(self) -> None:
        """提交当前线程连接上的事务；不影响其他线程。"""

        self._for_thread().commit()

    def rollback(self) -> None:
        """回滚当前线程连接上的事务；不影响其他线程。"""

        self._for_thread().rollback()

    def cursor(self, *args: Any) -> sqlite3.Cursor:
        """在当前线程的连接上创建游标。"""

        return self._for_thread().cursor(*args)

    def backup(self, target: Any, **kwargs: Any) -> None:
        """把当前线程连接看到的一致性快照备份到目标连接。"""

        self._for_thread().backup(target, **kwargs)

    def iterdump(self) -> Iterator[str]:
        """按当前线程连接的视角导出 SQL 文本。"""

        return self._for_thread().iterdump()

    def set_trace_callback(self, callback: Callable[[str], None] | None) -> None:
        """为所有连接安装语句回调，并记住它供后续新建的连接使用。

        :param callback: 每条语句执行前调用的回调；``None`` 表示取消。
        副作用：改写已存在的全部连接的回调设置。
        """

        self._trace = callback
        with self._guard:
            connections = list(self._connections)
        for connection in connections:
            connection.set_trace_callback(callback)

    @property
    def in_transaction(self) -> bool:
        """当前线程的连接上是否有未提交的事务。"""

        return self._for_thread().in_transaction

    @property
    def total_changes(self) -> int:
        """当前线程的连接自建立以来修改的行数。"""

        return self._for_thread().total_changes

    def __enter__(self) -> sqlite3.Connection:
        """进入当前线程连接的事务上下文。"""

        return self._for_thread().__enter__()

    def __exit__(self, *exc_info: Any) -> Any:
        """退出事务上下文：正常返回则提交，异常则回滚。"""

        return self._for_thread().__exit__(*exc_info)

    def __getattr__(self, name: str) -> Any:
        """把未显式实现的成员转发到当前线程的连接。

        :param name: ``sqlite3.Connection`` 的成员名。
        :return: 该成员在当前线程连接上的取值。
        :raises AttributeError: 连接本身没有该成员。

        这里刻意用属性转发而不是逐个包装：本类若漏包装某个成员，它会落到别的线程的
        连接上，且不报错——恰好是本类要消灭的那类静默错误。
        """

        if name.startswith('_'):
            raise AttributeError(name)
        return getattr(self._for_thread(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        """公开属性视为句柄级设置，写入全部连接并对后续新连接生效。"""

        if name.startswith('_'):
            object.__setattr__(self, name, value)
            return
        with self._guard:
            self._settings[name] = value
            connections = list(self._connections)
        for connection in connections:
            setattr(connection, name, value)

    def close(self) -> None:
        """关闭本句柄下的所有连接。

        :raises sqlite3.Error: 底层连接关闭失败时传播异常。
        副作用：其他线程此后再使用本句柄会拿到已关闭的连接并抛
            ``sqlite3.ProgrammingError``；这是使用错误，不做掩盖。
        """

        with self._guard:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close()


def open_db(path: str | Path) -> Database:
    """打开或返回进程级数据库句柄。

    DDL 和种子数据由迁移管理器在版本判断与备份之后执行，避免已有库提前应用目标
    结构导致迁移失败时无法恢复原始版本。

    :param path: SQLite 数据库文件路径，或 ``':memory:'``。

    :return: 进程级共享句柄；如果已经打开，则忽略本次路径并返回已有句柄。

    :raises sqlite3.Error: 数据库连接创建失败。

    副作用：
        首次调用建立句柄与创建者线程的连接，并保存模块级引用。
    """

    global _db
    if _db is not None:
        return _db
    _db = Database(path)
    return _db


def get_db() -> Database:
    """返回进程级数据库句柄。

    :return: 最近一次由 :func:`open_db` 打开的句柄。
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
        占用事件循环默认线程池线程。``fn`` 首次在某个池线程里用到数据库句柄时会为
        该线程建立一条连接，此后随线程复用。``fn`` 必须自己结束事务（``with db:``
        或显式 ``commit``）：它的未提交写入不再会被别的线程顺手提交。
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)


def close_db() -> None:
    """关闭进程级数据库句柄下的全部连接并清空单例引用。

    :return: 无返回值；未打开时安全返回。
    副作用：关闭所有线程的连接，后续调用 :func:`get_db` 会失败，直到重新调用
        :func:`open_db`。
    :raises sqlite3.Error: 底层连接关闭失败时传播异常。
    """
    global _db
    if _db is not None:
        _db.close()
        _db = None
