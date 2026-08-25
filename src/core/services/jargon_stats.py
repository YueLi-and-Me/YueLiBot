"""后台重建各会话的高频词表快照。

统计走旁路：生命周期回调驱动一个低频轮询任务，只重建有新消息的会话，
避免每半小时对全部历史做一遍分词。首轮启动时全量重建一次，让新部署
立刻有表可用；之后每轮只碰窗口内有新消息的会话。
"""

from __future__ import annotations

import asyncio
import sqlite3

from src.core.common.logger import get_logger
from src.core.memory.high_frequency import rebuild_stream_terms

logger = get_logger(__name__)

# 轮询间隔（秒）。高频词表给打分用，打分只关心量级差异；半小时级别的
# 滞后无害，而更密的轮询只是在重复分词同一批文本。
REFRESH_INTERVAL_SECONDS = 30 * 60


class JargonStatsService:
    """按会话重建 ``high_frequency_terms`` 的后台任务。"""

    def __init__(self, db: sqlite3.Connection) -> None:
        """保存数据库连接并初始化任务状态。

        :param db: 进程级 SQLite 连接（与 MemoryStore 同一来源）。
        副作用：只保存引用，不启动任务。
        """
        self._db = db
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # 上次全量重建以来见过的最新消息时间戳；None 表示尚未跑过首轮。
        self._watermark: int | None = None

    async def startup(self) -> None:
        """首轮全量重建，然后启动低频轮询任务。

        :return: ``None``。
        副作用：
            同步执行一次全量重建（各会话之间让出事件循环），随后创建
            名为 ``jargon-stats-poll`` 的后台任务。
        """
        self._stop.clear()
        await self._rebuild_all()
        self._task = asyncio.create_task(
            self._poll_loop(), name='jargon-stats-poll')

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
        """周期性重建有新消息的会话，直到收到停止信号。

        :return: ``None``。
        副作用：按轮询间隔重建高频词表；单个会话重建失败只记日志，
            不终止轮询。
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=REFRESH_INTERVAL_SECONDS)
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self._rebuild_changed()
            except sqlite3.Error as exc:
                logger.error('jargon_stats_rebuild_failed', error=str(exc))

    async def _rebuild_all(self) -> None:
        """对所有存在用户消息的会话重建一次快照。

        :return: ``None``。
        副作用：写 ``high_frequency_terms``；各会话之间 ``sleep(0)`` 让出
            事件循环，避免首轮全量分词卡住别的协程。
        """
        rows = self._db.execute(
            '''SELECT stream_id, MAX(created_at) AS latest FROM messages
               WHERE role = 'user' GROUP BY stream_id''',
        ).fetchall()
        rebuilt = 0
        for row in rows:
            count = rebuild_stream_terms(self._db, int(row['stream_id']))
            rebuilt += count
            latest = int(row['latest'])
            if self._watermark is None or latest > self._watermark:
                self._watermark = latest
            await asyncio.sleep(0)
        logger.info(
            'jargon_stats_initial_rebuild',
            streams=len(rows), terms=rebuilt,
        )

    async def _rebuild_changed(self) -> None:
        """只重建统计窗口内有新消息的会话。

        :return: ``None``。
        :raises sqlite3.Error: 查询活跃会话或重建快照失败。
        副作用：写 ``high_frequency_terms`` 并推进水位。
        """
        if self._watermark is None:
            await self._rebuild_all()
            return
        rows = self._db.execute(
            '''SELECT DISTINCT stream_id FROM messages
               WHERE role = 'user' AND created_at > ?''',
            (self._watermark,),
        ).fetchall()
        rebuilt = 0
        for row in rows:
            rebuilt += rebuild_stream_terms(self._db, int(row['stream_id']))
            await asyncio.sleep(0)
        if rows:
            fresh = self._db.execute(
                "SELECT MAX(created_at) FROM messages WHERE role = 'user'",
            ).fetchone()
            latest = int(fresh[0]) if fresh and fresh[0] is not None else self._watermark
            self._watermark = max(self._watermark, latest)
        logger.debug(
            'jargon_stats_refresh', streams=len(rows), terms=rebuilt)
