"""主动感知 tick 派发的日方向生成任务在关闭前的收束契约测试。

本模块只验证一处接缝：``AwarenessService._tick()`` 为日方向生成派发的后台 task
必须由该服务持有生命周期，且 ``AwarenessService.shutdown()`` 返回前必须把它收束
（自然完成并取回结果，或取消并等待取消完成）。日程生成器一律使用可控阻塞的桩件，
不访问真实模型；事件账本与数据库用可观测的假资源替代，用来证明关闭之后没有后台
写入越过生命周期边界。

依赖关系：``src.core.services.proactive.AwarenessService``（被测对象）、
``src.core.schedule.plan.DayPlanService``（真实生成链路的持有方）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import asyncio
import gc
import json

import pytest

import src.core.services.proactive as proactive_module
from src.core.config.schema import Config
from src.core.observe.store import event_store
from src.core.persona.state import PersonaState
from src.core.schedule.plan import DayPlanService, day_plan_date
from src.core.schedule.timeline import ActivityTimeline
from src.core.services.chat import ChatService
from src.core.services.proactive import AwarenessService


async def _noop_push(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    """忽略所有对外事件推送的桩件。"""

    return None


def _plan_body(date: str) -> str:
    """返回一份能通过本地结构校验的当日方向 JSON。"""

    return json.dumps(
        {
            'date': date,
            'theme': '把手上这件事收个尾。',
            'intentions': [
                {'what': '把手上的事收尾', 'carriedDays': 0},
                {'what': '整理零散的记录', 'carriedDays': 0},
                {'what': '给明天留一点余地', 'carriedDays': 0},
            ],
            'roughRhythm': '今天顺着实际状态安排节奏',
        },
        ensure_ascii=False,
    )


class _FakePlanStore:
    """带关闭开关的方向存储桩件，用来观测关闭后的后台写入。

    真实链路上，方向落库走记忆存储、观测事件走独立事件账本，两者都在
    ``storage`` 服务里关闭。本桩件把同一个契约显式化：资源关闭之后任何写入
    都必然失败，因此每一条关闭后的写入都是一次生命周期越界。
    """

    def __init__(self) -> None:
        self.values: Dict[str, Any] = {}
        self.accepting = True
        self.writes = 0
        self.writes_after_close = 0
        self.write_attempts_after_close = 0

    def read_json(self, key: str, fallback: Any) -> Any:
        return self.values.get(key, fallback)

    def write_json(self, key: str, value: Any) -> None:
        self.writes += 1
        if not self.accepting:
            self.write_attempts_after_close += 1
            raise RuntimeError('方向存储已关闭')
        self.writes_after_close = 0
        self.values[key] = value

    def close(self) -> None:
        """关闭存储，之后任何写入都拒绝。"""

        self.accepting = False


class _BlockingGenerator:
    """可控阻塞的日方向生成器：进入后停在 ``release`` 上，直到用例放行。"""

    def __init__(self, date: str) -> None:
        self.date = date
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.finished = 0
        self.cancelled = 0

    async def generate(self, _prompt: str) -> str:
        self.calls += 1
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.finished += 1
        return _plan_body(self.date)


class _ImmediateGenerator:
    """立即返回合法方向的生成器，用来验证生成自然完成后的收束。"""

    def __init__(self, date: str) -> None:
        self.date = date
        self.calls = 0

    async def generate(self, _prompt: str) -> str:
        self.calls += 1
        return _plan_body(self.date)


@dataclass
class _ClosedResources:
    """关闭事件账本与数据库之后的可观测状态。

    :ivar store: 方向存储桩件，其 ``write_attempts_after_close`` 即关闭后的写入次数。
    :ivar ledger_events: 关闭账本前已落账的事件数，用于证明关闭后没有新事件。
    """

    store: _FakePlanStore
    ledger_events: int


def _make_schedule(db: Any, store: _FakePlanStore, generator: Any) -> DayPlanService:
    """构造接通真实生成链路的日程服务。"""

    return DayPlanService(
        store=store,
        persona_state=lambda: PersonaState(
            intimacy=50.0,
            energy=60.0,
            mood=50.0,
            updated_at=0,
        ),
        interaction_density=lambda _now: '最近偶尔聊聊。',
        anniversary_at=lambda: 0,
        last_interaction_at=lambda: None,
        character_name='测试角色',
        character_personality='测试人设',
        generator=generator,
        db=db,
    )


def _make_service(db: Any, schedule: Any, cfg: Config | None = None) -> AwarenessService:
    """构造无前台信号源的主动感知服务，隔离日方向派发这一条接缝。"""

    settings = cfg or Config()
    chat = ChatService(db, None, None, None, _noop_push, cfg=settings)
    return AwarenessService(
        chat=chat,
        schedule=schedule,
        timeline=ActivityTimeline(db),
        cfg=settings,
        push_event=_noop_push,
        sensor=None,
    )


async def _wait_until(predicate: Any, message: str) -> None:
    """在事件循环里轮询条件成立，避免用固定 sleep 猜后台任务时序。"""

    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(message)


async def _wait_for_generation_entered(generator: _BlockingGenerator) -> None:
    """等待真实生成任务进入生成器，避免用固定 sleep 猜时序。

    这里必须让出事件循环：``_tick()`` 只负责把生成任务派发到循环上，生成器是在
    下一轮调度里才被进入的。
    """

    await _wait_until(
        lambda: generator.entered.is_set(),
        '后台日方向生成没有在预期时间内进入生成器',
    )


async def _drain_inflight(schedule: DayPlanService) -> None:
    """等待日程服务的在飞任务登记清空。"""

    await _wait_until(
        lambda: not _inflight(schedule),
        '日程服务的在飞生成任务没有在预期时间内清空',
    )


def _inflight(schedule: DayPlanService) -> List[asyncio.Task[Any]]:
    """读取日程服务登记的当日生成任务，用于断言真实任务的状态。"""

    return list(schedule._inflight.values())


def _close_resources(
    monkeypatch: pytest.MonkeyPatch,
    store: _FakePlanStore,
) -> _ClosedResources:
    """按真实 lifecycle 的行为关闭事件账本、数据库与方向存储。

    记录关闭时的账本事件数，之后可以精确判断“关闭后是否还有事件写入”。
    """

    events_before = len(event_store.since(0, 1_000).events)
    store.close()
    from src.core.db.connection import close_db

    close_db()
    event_store.close()
    # 关闭之后所有时间结算都必然失败，与真实进程的退出阶段一致。
    monkeypatch.setattr(proactive_module, 'current_time', lambda: (_ for _ in ()).throw(
        RuntimeError('数据库已关闭'),
    ))
    return _ClosedResources(store=store, ledger_events=events_before)


def _leaked_store_writes(resources: _ClosedResources) -> int:
    """返回关闭之后仍然尝试写入方向存储的次数。"""

    return resources.store.write_attempts_after_close


class _ImmediateFailureSchedule:
    """``ensure()`` 立即抛异常的最小日程桩件。

    用来把「任务在 shutdown 之前自行异常完成」这一条路径单独隔离出来，不牵动真实
    生成链路。异常类型与消息固定，便于断言事件循环捕获到的是同一个异常。
    """

    MESSAGE = 'probe-immediate-failure'

    async def ensure(self, _now: Any = None) -> Any:
        """立即抛出 RuntimeError，不产生任何 await 让出点。"""

        raise RuntimeError(self.MESSAGE)


class _LoopExceptionSpy:
    """替换事件循环的异常处理器，记录未取回异常。

    只读观测：不使用 ``task.exception()`` 统计未取回异常，因为那一次调用本身就会
    把异常标记为已取回，从而改变被观测状态。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._previous = loop.get_exception_handler()
        self.contexts: List[Dict[str, Any]] = []

    def install(self) -> None:
        """接管异常处理器。"""

        self._loop.set_exception_handler(self._handle)

    def restore(self) -> None:
        """还原原处理器，避免影响其他用例。"""

        self._loop.set_exception_handler(self._previous)

    def _handle(self, _loop: Any, context: Dict[str, Any]) -> None:
        self.contexts.append(dict(context))

    def never_retrieved(self) -> List[Tuple[str, BaseException]]:
        """返回被事件循环判定为「异常未取回」的 (消息, 异常) 列表。"""

        found: List[Tuple[str, BaseException]] = []
        for context in self.contexts:
            message = str(context.get('message', ''))
            if 'never retrieved' not in message:
                continue
            error = context.get('exception')
            found.append((message, error if isinstance(error, BaseException) else Exception('')))
        return found


def _capture_loop_exception_count(records: List[Tuple[str, BaseException]]) -> int:
    """把捕获记录折算成未取回异常数。"""

    return len(records)


# ---------------------------------------------------------------- 红测：未取回异常


# ---------------------------------------------------------------- 红测：未取回异常


async def test_deferred_schedule_failure_is_retrieved_and_logged_once(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """派发出去的生成任务自行异常完成时，异常必须由持有方取回并记录一次。

    竞态与缺陷：``_tick`` 把 task 交给 ``add_done_callback(集合.discard)``。任务在
    shutdown 之前自行异常结束时，回调只把它移出集合，没有任何一方取回异常，于是
    事件循环在任务被回收时报 ``Task exception was never retrieved``。

    本用例刻意不 await 该 Task、也不调用 ``result()`` / ``exception()``：那两种做法
    都会替生产代码取回异常，从而把缺陷掩盖掉。观测点只有事件循环的异常处理器。
    """

    loop = asyncio.get_running_loop()
    spy = _LoopExceptionSpy(loop)
    spy.install()
    monkeypatch.setattr(proactive_module, 'current_time', lambda: 1_789_000_000_000)
    service = _make_service(db, _ImmediateFailureSchedule())
    try:
        await service._tick()
        # 让任务跑起来、结束并被回收：未取回异常正是在回收时由事件循环报出。
        for _ in range(10):
            await asyncio.sleep(0)
        gc.collect()
        for _ in range(10):
            await asyncio.sleep(0)
        records = spy.never_retrieved()
        captured = _capture_loop_exception_count(records)
        remaining = len(service._ensure_tasks)
        output = capsys.readouterr().out
        logged = output.count('awareness_schedule_task_failed')
        # 任务已由完成回调取回，drain 不应把它再算作在飞任务、也不应重复记录。
        await service.shutdown()
        after_shutdown_output = capsys.readouterr().out
        logged_after_shutdown = after_shutdown_output.count('awareness_schedule_task_failed')
        with capsys.disabled():
            print(
                '[shutdown-drain] 未取回异常={captured} 错误日志={logged} '
                '在册任务={remaining} shutdown后新增日志={extra} 消息={messages}'.format(
                    captured=captured,
                    logged=logged,
                    remaining=remaining,
                    extra=logged_after_shutdown,
                    messages=[message for message, _ in records],
                ),
            )
    finally:
        spy.restore()

    assert captured == 0, f'任务异常无人取回：{records}'
    assert logged == 1, f'异常没有恰好记录一次，实际 {logged} 次'
    assert logged_after_shutdown == 0, 'shutdown drain 重复记录了同一个异常'
    assert _ImmediateFailureSchedule.MESSAGE in output
    assert remaining == 0, '已完成的任务仍留在持有集合里'


# ---------------------------------------------------------------- 红测：关闭前收束


async def test_shutdown_drains_inflight_schedule_generation(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生成在飞时 shutdown 必须取消并等待完成，之后的资源关闭不得再被写入。

    严格按真实 lifecycle 的顺序取材：tick 派发并进入生成 → ``awareness.shutdown()``
    返回 → 事件账本与数据库关闭 → 才放行生成同步点。这样「关闭后写入」的观测窗口
    才与真机一致；先关资源再调 shutdown 会把顺序反过来，测不到越界写入。

    竞态与缺陷：``_tick()`` 曾用裸 ``create_task`` 派发 ``schedule.ensure(now)``，
    真实生成任务登记在 ``DayPlanService._inflight`` 里；``shutdown()`` 只收束
    轮询 task，于是生成任务越过关闭边界继续跑，并在事件账本与数据库关闭后落库。
    """

    now = proactive_module.current_time()
    store = _FakePlanStore()
    generator = _BlockingGenerator(day_plan_date(now))
    schedule = _make_schedule(db, store, generator)
    service = _make_service(db, schedule)
    monkeypatch.setattr(proactive_module, 'current_time', lambda: now)

    await asyncio.wait_for(service.startup(), timeout=1.0)
    await _wait_for_generation_entered(generator)
    inflight = _inflight(schedule)
    assert len(inflight) == 1
    task = inflight[0]

    # 1. awareness 先停：真实 lifecycle 里 awareness 早于 storage 关闭。
    await asyncio.wait_for(service.shutdown(), timeout=5.0)
    task_done_after_shutdown = task.done()

    # 2. 之后才关闭事件账本、数据库与方向存储。
    resources = _close_resources(monkeypatch, store)

    # 3. 放行生成同步点：修复前任务会在这里写进已关闭的资源。
    generator.release.set()
    for _ in range(20):
        await asyncio.sleep(0)

    assert task_done_after_shutdown, 'shutdown 返回后日方向生成任务仍然 pending'
    assert generator.cancelled == 1, 'shutdown 没有真正取消在飞的生成任务'
    assert _leaked_store_writes(resources) == 0, '关闭后仍有后台方向写入'
    # 账本连接已经关闭，关闭后的写入必然抛「事件账本尚未配置」，因此方向存储的
    # 关闭后写入次数就是本用例可精确观测的副作用计数。
    assert resources.store.writes == 0, '关闭后仍未停止后台写入'


async def test_shutdown_reports_task_state_and_side_effects(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """打印收束口径：任务 done/pending、未取回异常数与关闭后副作用数。

    这几项是关闭竞态的验收数字，随用例输出留档，避免只用「通过」掩盖真实状态。
    未取回异常数只从事件循环的异常处理器读取，不用 ``task.exception()`` 统计——那一次
    调用本身就会把异常标记为已取回，从而改变被观测状态。
    """

    loop = asyncio.get_running_loop()
    spy = _LoopExceptionSpy(loop)
    spy.install()
    try:
        now = proactive_module.current_time()
        store = _FakePlanStore()
        generator = _BlockingGenerator(day_plan_date(now))
        schedule = _make_schedule(db, store, generator)
        service = _make_service(db, schedule)
        monkeypatch.setattr(proactive_module, 'current_time', lambda: now)

        await asyncio.wait_for(service.startup(), timeout=1.0)
        await _wait_for_generation_entered(generator)
        tracked = list(service._ensure_tasks)
        assert len(tracked) == 1
        task = tracked[0]

        await asyncio.wait_for(service.shutdown(), timeout=5.0)
        pending_after_shutdown = sum(1 for item in tracked if not item.done())
        unretrieved = _capture_loop_exception_count(spy.never_retrieved())
        task_done = task.done()

        # 放行已取消的生成，确认关闭之后没有产生任何写入。
        generator.release.set()
        await asyncio.gather(*tracked, return_exceptions=True)
        effects_after_shutdown = store.write_attempts_after_close
        remaining_tracked = len(service._ensure_tasks)

        with capsys.disabled():
            print(
                '[shutdown-drain] 目标任务 done={done} pending={pending} 未取回异常={unretrieved} '
                '关闭后副作用={effects} 在册任务={remaining}'.format(
                    done=task_done,
                    pending=pending_after_shutdown,
                    unretrieved=unretrieved,
                    effects=effects_after_shutdown,
                    remaining=remaining_tracked,
                ),
            )
    finally:
        spy.restore()

    assert task_done
    assert pending_after_shutdown == 0
    assert unretrieved == 0
    assert effects_after_shutdown == 0
    assert remaining_tracked == 0


async def test_shutdown_leaves_no_pending_task_when_generation_completes_with_shutdown(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生成恰好与 shutdown 同时自然完成时，任务必须已完成且结果已取回。

    这一条覆盖“自然完成”分支：取消与正常返回在同一轮事件循环里竞争，收束逻辑
    不允许把已完成的任务当作待取消任务处理，也不允许留下未取回的异常。
    """

    now = proactive_module.current_time()
    store = _FakePlanStore()
    generator = _BlockingGenerator(day_plan_date(now))
    schedule = _make_schedule(db, store, generator)
    service = _make_service(db, schedule)
    monkeypatch.setattr(proactive_module, 'current_time', lambda: now)

    await asyncio.wait_for(service.startup(), timeout=1.0)
    await _wait_for_generation_entered(generator)
    task = _inflight(schedule)[0]

    # 先落库再停止：生成在这一瞬间自然完成，生成器不再进入取消分支。
    generator.release.set()
    await asyncio.wait_for(task, timeout=2.0)
    await asyncio.wait_for(service.shutdown(), timeout=5.0)

    assert task.done()
    assert not task.cancelled()
    assert task.exception() is None
    assert generator.finished == 1
    assert store.values, '自然完成的方向没有落库'
    assert task not in _inflight(schedule)


async def test_shutdown_without_schedule_and_with_settled_plan_is_clean(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无日程服务、以及当日方向已生成且在飞任务为零时，shutdown 都要正常返回。"""

    now = proactive_module.current_time()
    monkeypatch.setattr(proactive_module, 'current_time', lambda: now)

    headless = _make_service(db, None)
    await asyncio.wait_for(headless.startup(), timeout=1.0)
    await asyncio.wait_for(headless.shutdown(), timeout=5.0)
    assert headless._poll_task is None

    store = _FakePlanStore()
    generator = _ImmediateGenerator(day_plan_date(now))
    schedule = _make_schedule(db, store, generator)
    service = _make_service(db, schedule)
    await asyncio.wait_for(service.startup(), timeout=1.0)
    for _ in range(200):
        if generator.calls:
            break
        await asyncio.sleep(0)
    assert generator.calls == 1
    await _drain_inflight(schedule)
    assert _inflight(schedule) == []

    await asyncio.wait_for(service.shutdown(), timeout=5.0)
    assert service._poll_task is None
    assert store.values, '自然完成的方向没有落库'


# ---------------------------------------------------------------- 停止后不再派生


async def test_tick_after_stop_does_not_start_generation(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop 已设置后，tick 不得再派生新的日方向生成任务。"""

    now = proactive_module.current_time()
    store = _FakePlanStore()
    generator = _ImmediateGenerator(day_plan_date(now))
    schedule = _make_schedule(db, store, generator)
    service = _make_service(db, schedule)
    monkeypatch.setattr(proactive_module, 'current_time', lambda: now)

    await asyncio.wait_for(service.startup(), timeout=1.0)
    service._stop.set()

    for _ in range(3):
        await service._tick()
    for _ in range(50):
        await asyncio.sleep(0)

    assert generator.calls == 0, 'stop 已设置后仍然派发了日方向生成'
    assert _inflight(schedule) == []
    assert store.writes == 0

    await asyncio.wait_for(service.shutdown(), timeout=5.0)


async def test_repeated_ticks_start_one_generation_per_date(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一天多次 tick 只允许启动一次真实生成，去重语义不能改变。"""

    now = proactive_module.current_time()
    store = _FakePlanStore()
    generator = _BlockingGenerator(day_plan_date(now))
    schedule = _make_schedule(db, store, generator)
    service = _make_service(db, schedule)
    monkeypatch.setattr(proactive_module, 'current_time', lambda: now)

    await service._tick()
    await _wait_for_generation_entered(generator)
    for _ in range(3):
        await service._tick()
    for _ in range(20):
        await asyncio.sleep(0)

    assert generator.calls == 1, f'同一天底层生成器被调用 {generator.calls} 次'

    generator.release.set()
    await _drain_inflight(schedule)
    assert generator.calls == 1
    assert len(store.values) == 1


# ---------------------------------------------------------------- 真实错误仍可观察


async def test_generation_error_stays_observable_through_shutdown(
    db: Any,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实生成异常必须仍然可观察，不能被 shutdown 收束逻辑静默吞掉。"""

    now = proactive_module.current_time()
    store = _FakePlanStore()

    class _FailingGenerator:
        async def generate(self, _prompt: str) -> str:
            raise RuntimeError('日程模型暂时不可用')

    schedule = _make_schedule(db, store, _FailingGenerator())
    service = _make_service(db, schedule)
    monkeypatch.setattr(proactive_module, 'current_time', lambda: now)

    await service._tick()
    # 派发出去的生成任务必须由服务持有；等它自然跑完再验证异常可观察性。
    tasks = list(service._ensure_tasks)
    assert len(tasks) == 1, 'tick 没有派发或被服务持有的日方向生成任务'
    await asyncio.wait_for(tasks[0], timeout=2.0)
    await asyncio.wait_for(service.shutdown(), timeout=5.0)
    issue = schedule.generation_issue(now)
    output = capsys.readouterr().out
    assert issue is not None and issue.kind == 'provider-error'
    assert '日程模型暂时不可用' in (issue.reason or '')
    assert '方向生成失败' in output, '生成失败没有留下可观察日志'


# ---------------------------------------------------------------- _tick 时序接缝


async def test_tick_settles_time_before_first_activity_read(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """settle_time 必须早于第一次活动读取与活动状态刷新。

    精力结算要读到最新的活动边界决策，顺序反了会把离线时段按旧节奏结算掉。
    """

    now = proactive_module.current_time()
    order: List[str] = []
    timeline = ActivityTimeline(db)
    real_current = timeline.current

    def _spy_current(at: int) -> Any:
        order.append('current')
        return real_current(at)

    timeline.current = _spy_current  # type: ignore[method-assign]

    class _ProbeMemory:
        def load_pending_promises(self) -> List[Any]:
            return []

        def last_message_at(self, _stream_id: int) -> None:
            return None

    class _ProbeChat:
        """只保留 ``startup`` 注册时用到的记忆接口，其余时序由记录器观测。"""

        ready = False

        def __init__(self) -> None:
            self.memory = _ProbeMemory()

        async def summarize_deep_sleep(self, _state: Any) -> None:
            order.append('summarize')

        def settle_time(self, _at: int) -> None:
            order.append('settle_time')

        def set_activity_provider(self, _provider: Any) -> None:
            return None

        def set_sleep_state_provider(self, _provider: Any) -> None:
            return None

        def set_sleep_wake_handler(self, _handler: Any) -> None:
            return None

        def set_promise_handler(self, _handler: Any) -> None:
            return None

    service = AwarenessService(
        chat=_ProbeChat(),
        schedule=None,
        timeline=timeline,
        cfg=Config(),
        push_event=_noop_push,
        sensor=None,
    )
    monkeypatch.setattr(proactive_module, 'current_time', lambda: now)

    await service.startup()
    # 轮询循环是在 startup 之后才被调度的；先等第一次 tick 真正跑完，再验证顺序。
    await _wait_until(
        lambda: 'summarize' in order,
        '轮询循环没有在预期时间内执行第一次 tick',
    )
    await asyncio.wait_for(service.shutdown(), timeout=5.0)

    assert order, 'tick 没有读取活动时间线'
    assert order[0] == 'settle_time', f'tick 时序被改变：{order}'
    assert 'current' in order, 'tick 没有在结算之后读取当前活动'
    assert 'summarize' in order, 'tick 没有刷新活动状态'


async def test_tick_does_not_block_on_generation(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日方向生成必须留在后台，不得用模型延迟阻塞 minute tick。"""

    now = proactive_module.current_time()
    store = _FakePlanStore()
    generator = _BlockingGenerator(day_plan_date(now))
    schedule = _make_schedule(db, store, generator)
    service = _make_service(db, schedule)
    monkeypatch.setattr(proactive_module, 'current_time', lambda: now)

    # tick 必须在生成器仍被阻塞时返回；生成器是否已进入由下一轮调度决定。
    await asyncio.wait_for(service._tick(), timeout=1.0)
    await _wait_for_generation_entered(generator)

    assert generator.finished == 0, 'tick 在生成完成后才返回，日方向阻塞了轮询'
    assert _inflight(schedule), 'tick 没有在后台派发日方向生成'

    generator.release.set()
    await _drain_inflight(schedule)
    assert generator.finished == 1
