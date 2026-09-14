"""进程级 SQLite 连接的事务边界不变量。

SQLite 的事务状态挂在连接上而不是线程上，而 ``src/core/db/connection.py`` 的句柄同时
服务事件循环线程和 ``run_in_thread`` 的线程池。本模块把「一个写入单元要么整体可见、
要么整体不可见」写成三条可执行断言，外加一条针对连接级设置的断言：

- 单线程内第二条语句失败时，第一条不得留在库里（``Persona._write`` 的文档声称原子）；
- 写入单元进行到一半时，另一线程读到的字段必须同代；
- 另一线程的 ``commit`` 不得替别人把半截写入落盘，使其 ``rollback`` 失效；
- 新线程自动获得的连接必须带齐 ``foreign_keys`` 与 ``busy_timeout``。

前三条不依赖具体修法：既能证伪「只给 ``_write`` 补 ``with self._db:``」这种只治单线程
的改法（第二、三条仍红），也能验收按线程分连接。被测目标是
``src/core/persona/state.Persona`` 这个最短的双语句写入单元，结论对所有共用该句柄的
写入路径成立。
"""

from pathlib import Path
from typing import Any, Iterator

import sqlite3
import threading

import pytest

from src.core.db import connection as connection_module
from src.core.db.connection import BUSY_TIMEOUT_MS, close_db, open_db
from src.core.db.migrations.manager import run_migrations
from src.core.persona.state import Persona, PersonaState

# 对照线程等待写入线程的上限；超时即判定用例自身失效，不得静默通过。
HANDOFF_TIMEOUT_S = 5.0


@pytest.fixture()
def file_db(tmp_path: Path) -> Iterator[Any]:
    """提供一个落盘的数据库连接，而不是 conftest 的 ``:memory:``。

    并发语义必须在真实文件上验证：内存库无法表达 WAL 下「读者不被写者阻塞」，
    而修法若改为线程局部连接，``:memory:`` 的每条连接还会各自指向不同的空库。
    """

    connection_module._db = None
    path = tmp_path / 'yueli.db'
    db = open_db(path)
    run_migrations(db, db_path=None)
    yield db
    close_db()
    connection_module._db = None


class _PausedBeforeSecondStatement:
    """把一个写入单元在两条语句之间停住的连接替身。

    只拦截 ``execute`` 的调度时机，不改写 SQL、不改动参数、不吞异常，因此被测代码
    的语义与真实运行完全一致；其余属性透传给真实连接对象。用它而不是
    ``set_trace_callback`` 是必须的：
    - 现象：在 trace 回调里阻塞会让另一线程的 ``execute`` 一起卡死。
    - 原因：trace 回调由 ``sqlite3_step`` 内部调起，此时连接级互斥锁仍被持有。
    - 后果：用回调实现交错点会把用例变成死锁，观察不到任何断言。
    """

    def __init__(
        self, real: Any, marker: str, entered: threading.Event, resume: threading.Event,
    ) -> None:
        """绑定真实连接与交错点。

        :param real: 被代理的连接对象。
        :param marker: 命中即暂停的 SQL 片段；只对第一次命中生效。
        :param entered: 暂停生效后置位，供对照线程确认写入单元已进行到一半。
        :param resume: 由对照线程置位以释放写入线程。
        """

        self._real = real
        self._marker = marker
        self._entered = entered
        self._resume = resume
        self._tripped = False

    def execute(self, sql: str, *args: Any) -> Any:
        """在首次命中 ``marker`` 的语句执行之前让出控制权。"""

        if not self._tripped and self._marker in sql:
            self._tripped = True
            self._entered.set()
            assert self._resume.wait(HANDOFF_TIMEOUT_S), '对照线程未在超时内释放写入线程'
        return self._real.execute(sql, *args)

    def __enter__(self) -> Any:
        return self._real.__enter__()

    def __exit__(self, *exc: Any) -> Any:
        return self._real.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _owner_id(db: Any) -> int:
    """取出迁移种子里的 owner 主键。"""

    return int(db.execute("SELECT id FROM persons WHERE kind = 'owner'").fetchone()[0])


def _bond_intimacy(db: Any, person_id: int) -> float:
    """读取指定人物当前落盘的亲密度。"""

    return float(
        db.execute(
            'SELECT intimacy FROM persona_bond WHERE person_id = ?', (person_id,),
        ).fetchone()[0]
    )


def _unbindable_state(base: PersonaState, intimacy: float) -> PersonaState:
    """构造一个第一条语句合法、第二条语句必然绑参失败的目标状态。

    ``_write`` 先写 ``persona_bond.intimacy``（合法浮点），再写
    ``persona_self.energy``；把 energy 换成 SQLite 无法适配的对象，失败点就精确落在
    两条语句之间，不需要伪造异常。
    """

    return PersonaState(intimacy, object(), base.mood, base.updated_at + 1)  # type: ignore[arg-type]


def test_every_thread_connection_carries_the_same_pragmas() -> None:
    """新线程自动获得的连接必须带齐连接级设置。

    ``foreign_keys`` 与 ``busy_timeout`` 都属于连接而不属于数据库文件。DDL 里那条
    ``PRAGMA foreign_keys = ON`` 只对执行它的连接生效，新连接漏设会让外键约束在别的线程
    上静默失效；``busy_timeout`` 是跨线程写者的等待上限，钉在模块常量上是为了让改动连接
    构造的人无法顺手把它丢回库的默认值。用例在 :func:`file_db` 之外自建句柄，避免夹带
    fixture 的连接计数。
    """

    def _pragmas() -> tuple[int, int]:
        return (
            int(handle.execute('PRAGMA foreign_keys').fetchone()[0]),
            int(handle.execute('PRAGMA busy_timeout').fetchone()[0]),
        )

    connection_module._db = None
    handle = open_db(':memory:')
    try:
        assert _pragmas() == (1, BUSY_TIMEOUT_MS)
        observed: dict[str, tuple[int, int]] = {}
        worker = threading.Thread(target=lambda: observed.update(value=_pragmas()))
        worker.start()
        worker.join(HANDOFF_TIMEOUT_S)
        assert not worker.is_alive()
        assert observed['value'] == (1, BUSY_TIMEOUT_MS)
        assert handle.thread_connection_count == 2
    finally:
        close_db()
        connection_module._db = None


def test_write_leaves_no_half_applied_row_when_second_statement_fails(file_db: Any) -> None:
    """第二条语句失败时，第一条语句的写入不得被后来的任何提交落盘。"""

    person_id = _owner_id(file_db)
    persona = Persona(file_db)
    before = persona.get(person_id)

    with pytest.raises(sqlite3.Error):
        persona._write(person_id, _unbindable_state(before, before.intimacy + 5.0))

    # 进程里任何其他代码路径的提交都会替这半截事务落盘，这里代表那一次提交。
    file_db.commit()
    assert _bond_intimacy(file_db, person_id) == pytest.approx(before.intimacy)


def test_concurrent_reader_never_sees_half_applied_unit(file_db: Any) -> None:
    """写入单元进行到一半时，另一线程读到的亲密度与精力必须同代。"""

    person_id = _owner_id(file_db)
    persona = Persona(file_db)
    before = persona.get(person_id)
    file_db.execute('UPDATE persona_self SET energy = 40.0, mood = 50.0 WHERE id = 1')
    file_db.commit()

    entered = threading.Event()
    resume = threading.Event()
    persona._db = _PausedBeforeSecondStatement(file_db, 'persona_self', entered, resume)
    target = PersonaState(before.intimacy + 5.0, 80.0, 50.0, before.updated_at + 1)
    failures: list[BaseException] = []

    def _writer() -> None:
        try:
            persona._write(person_id, target)
        except BaseException as exc:   # noqa: BLE001 - 线程内异常必须带回主线程断言
            failures.append(exc)

    writer = threading.Thread(target=_writer, name='persona-writer')
    writer.start()
    assert entered.wait(HANDOFF_TIMEOUT_S), '写入线程未在超时内到达交错点'

    observed = file_db.execute(
        'SELECT b.intimacy, s.energy FROM persona_bond b, persona_self s '
        'WHERE b.person_id = ? AND s.id = 1',
        (person_id,),
    ).fetchone()

    resume.set()
    writer.join(HANDOFF_TIMEOUT_S)
    assert not writer.is_alive()
    assert not failures, failures

    seen = (float(observed[0]), float(observed[1]))
    assert (
        seen == pytest.approx((before.intimacy, 40.0))
        or seen == pytest.approx((target.intimacy, target.energy))
    ), f'读到跨代组合 {seen}：亲密度已更新而精力仍是旧值'


def test_other_threads_commit_cannot_persist_a_partial_unit(file_db: Any) -> None:
    """另一线程退出 ``with db:`` 时的提交，不得使别人半截写入落盘。

    对照线程刻意只读不写：``with db:`` 退出时无条件调用 ``commit()``，块内有没有写入
    都一样，这正是渗透的触发方式。共用连接时这次提交会把写入线程的第一条 UPDATE 落
    盘，此后写入线程自己的 ``rollback()`` 只剩一半可撤。对照线程若改成写入，两个写事
    务会在文件锁上真正互斥，反而观察不到本用例要钉的那件事。
    """

    person_id = _owner_id(file_db)
    persona = Persona(file_db)
    before = persona.get(person_id)

    entered = threading.Event()
    resume = threading.Event()
    persona._db = _PausedBeforeSecondStatement(file_db, 'persona_self', entered, resume)
    broken = _unbindable_state(before, before.intimacy + 7.0)
    rolled_back = threading.Event()

    def _writer() -> None:
        try:
            persona._write(person_id, broken)
        except sqlite3.Error:
            # 写入方发现单元失败后回滚自己的事务，这是它能做的全部。
            persona._db.rollback()
            rolled_back.set()

    writer = threading.Thread(target=_writer, name='persona-writer')
    writer.start()
    assert entered.wait(HANDOFF_TIMEOUT_S), '写入线程未在超时内到达交错点'

    with file_db:
        file_db.execute('SELECT COUNT(*) FROM persona_bond').fetchone()

    resume.set()
    writer.join(HANDOFF_TIMEOUT_S)
    assert not writer.is_alive()
    assert rolled_back.is_set(), '写入单元未按预期失败'

    assert _bond_intimacy(file_db, person_id) == pytest.approx(before.intimacy)
