"""后台冻结联想层里久未被强化的边。

联想层的边与事实共用同一套衰减语义（只冻结、不删除，再次共同出现时由
``link_together`` 复活），但两者的时间尺度差一个量级，因此不共用一个调度点：

- 事实走 ``MemoryStore.sweep``，由 ``due_at`` 列驱动、走索引，挂在回合路径上；
- 边没有 ``due_at`` 列，:func:`~src.core.memory.association.decay_edges` 是一次
  全表扫描。边的半衰期是 720 小时，一条首次建立的边（强度 ``EDGE_BOOST = 0.35``）
  要 1301 小时（约 54 天）不被强化才会跌破冻结阈值——用回合级精度去追一个以月为
  单位的量没有意义，为它加一列和一条迁移则是过度设计。

因此这里用一个低频轮询任务承载它。被 ``src.main`` 通过生命周期注册，
形态与 ``src.core.services.jargon_stats`` 一致。
"""

from __future__ import annotations

import asyncio
import sqlite3

from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.memory.association import decay_edges

logger = get_logger(__name__)

# 轮询间隔（秒）。取 6 小时是因为边的判据以月为单位：任何远小于半衰期的间隔
# 结果都一样，间隔只决定「边跌破阈值之后多久才被标记」的滞后上限。
SWEEP_INTERVAL_SECONDS = 6 * 60 * 60


class EdgeDecayService:
    """周期性冻结 ``memory_edges`` 中留存度跌破阈值的边。"""

    def __init__(self, db: sqlite3.Connection) -> None:
        """保存数据库连接并初始化任务状态。

        :param db: 进程级 SQLite 连接（与 MemoryStore 同一来源）。
        副作用：只保存引用，不启动任务。
        """
        self._db = db
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        """先扫一次，再启动低频轮询任务。

        启动时扫一次是必要的：进程停机期间边照常在衰减，长时间不开机的部署
        重新启动后应当立刻收敛，而不是再等一个轮询周期。

        :return: ``None``。
        副作用：同步执行一次冻结扫描，随后创建名为 ``edge-decay-sweep`` 的后台任务。
        """
        self._stop.clear()
        self._sweep_once()
        self._task = asyncio.create_task(
            self._poll_loop(), name='edge-decay-sweep')

    async def shutdown(self) -> None:
        """请求停止轮询并等待任务退出。

        :return: ``None``。
        副作用：设置停止事件；已结束的任务不重复等待。
        """
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        """按间隔重复扫描，直到收到停止信号。

        :return: ``None``。
        副作用：按轮询间隔冻结到期的边；扫描失败只记日志，不终止轮询。
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=SWEEP_INTERVAL_SECONDS)
                break
            except asyncio.TimeoutError:
                pass
            try:
                self._sweep_once()
            except sqlite3.Error as exc:
                logger.error('edge_decay_failed', error=str(exc))

    def _sweep_once(self) -> None:
        """执行一次冻结扫描并记录结果。

        :return: ``None``。
        :raises sqlite3.Error: 读写 ``memory_edges`` 失败。
        副作用：更新 ``memory_edges.active`` 并提交事务。
        """
        frozen = decay_edges(self._db, current_time())
        # 冻结数为 0 是常态（边要约 54 天不被强化才够得着阈值），因此走 debug；
        # 真的冻结了才值得在控制台留一行。
        if frozen:
            logger.info('edge_decay_frozen', edges=frozen)
        else:
            logger.debug('edge_decay_idle')
