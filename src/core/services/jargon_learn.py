"""黑话学习的后台服务：游标轮询、批次调度与推断节流。

形态照 :class:`src.core.services.jargon_stats.JargonStatsService`：生命周期
回调只负责起停一个后台任务，``startup`` 里 ``create_task`` 后立即返回——
把无限循环本身注册成 startup 钩子会让生命周期永久 ``await`` 下去，其后所有
服务都起不来。

本层不做业务判断：提取、证据与三步推断的语义都在
:mod:`src.core.agent.jargon_mine`，可独立单测。这里只回答三个调度问题：

- 何时学：每个会话一条独立游标（``meta`` 键 ``jargon_learn_cursor``），
  攒够一批消息才发起提取；提取失败不推进游标，下轮重跑同一批。
- 学多少：每轮提取调用数与推断词条数都有上限常量。三步推断一次是
  三次模型调用、阶梯四档最多十二次，真机千级存量词条一旦同时越过第一档，
  不节流就是四位数的调用量。
- 推断谁：每轮从 :func:`select_inference_targets` 取证据最多的若干条，
  解析失败者本轮在进程内跳过（重启后重试：模型输出有随机性）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Sequence, Set

from src.core.agent.jargon_mine import (
    advance_cursor,
    infer_term,
    jargon_learn_enabled,
    mine_batch,
    read_cursor,
    lock_name_collisions,
    select_inference_targets,
)
from src.core.common.logger import get_logger
from src.core.llm_models.protocol import LlmProvider
from src.core.memory.store import MemoryStore

logger = get_logger(__name__)

# 轮询间隔（秒）。学习不是回合内路径，分钟级滞后无害；首轮启动立即先跑
# 一轮再进入间隔等待，让存量语料尽快开始积累证据。
POLL_INTERVAL_SECONDS = 45

# 提取触发阈值与单批消息条数：攒够 40 条才值得一次模型往返，单批至多
# 100 条控制提示词长度。两者之差让「触发时批次不满」不会发生。
MIN_BATCH_MESSAGES = 40
MAX_BATCH_MESSAGES = 100

# 每轮（一次轮询）最多发起多少次提取模型调用。上限保证存量回放期不会
# 连打模型；正常运行时一轮很难攒出一个满批，这个上限基本不生效。
EXTRACTION_CALLS_PER_TICK = 6

# 每轮最多推断多少个词条。三步推断是三次模型调用，这个上限就是每轮
# 推断层最多 18 次模型调用的闸门。
INFERENCES_PER_TICK = 6


class JargonLearnService:
    """驱动黑话学习的后台轮询任务。"""

    def __init__(
        self,
        db: sqlite3.Connection,
        store: MemoryStore,
        provider: LlmProvider,
        *,
        temperature: float,
        max_tokens: int | None,
        bot_name: str,
        bot_names: Sequence[str],
    ) -> None:
        """保存数据库、存储、模型客户端与名字守卫所需信息。

        :param db: 进程级 SQLite 连接（与 MemoryStore 同一来源）。
        :param store: 聊天服务已创建的 MemoryStore，游标与批次读取走它。
        :param provider: 学习任务的模型客户端（memory 任务槽）。
        :param temperature: 模型采样温度。
        :param max_tokens: 模型输出上限；``None`` 表示由提供者决定。
        :param bot_name: bot 展示名，渲染语料时标记 Bot 自己的发言。
        :param bot_names: 名字守卫用的 bot 名字族（主名、别名、对用户的称呼）。
        副作用：只保存引用与初始化任务状态，不启动任务。
        """

        self._db = db
        self._store = store
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._bot_name = bot_name
        self._bot_names = tuple(bot_names)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # 本进程内解析失败被拉黑的词条 id 集合；重启清零，模型输出有随机性，
        # 换一次采样往往就解析成功了。
        self._poisoned: Set[int] = set()

    async def startup(self) -> None:
        """启动后台轮询任务并立即返回。

        :return: ``None``。
        副作用：创建名为 ``jargon-learn-poll`` 的后台任务；重复启动先复位
            停止事件。
        """

        self._stop.clear()
        self._task = asyncio.create_task(
            self._poll_loop(), name='jargon-learn-poll')

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
        """先跑一轮再按间隔轮询，直到收到停止信号。

        :return: ``None``。
        副作用：按 :data:`POLL_INTERVAL_SECONDS` 周期执行学习轮次；单轮
            失败只记日志，不终止轮询。
        """

        logger.info('jargon_learn_started', pollSeconds=POLL_INTERVAL_SECONDS)
        while not self._stop.is_set():
            try:
                await self._tick()
            except sqlite3.Error as exc:
                logger.error('jargon_learn_tick_failed', error=str(exc))
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=POLL_INTERVAL_SECONDS)
                break
            except asyncio.TimeoutError:
                continue
        logger.info('jargon_learn_stopped')

    async def _tick(self) -> None:
        """一轮学习：先提取后推断，两条腿都按上限节流。

        :return: ``None``。
        :raises sqlite3.Error: 枚举活跃会话或读取游标失败时抛出，
            由轮询层记录。
        副作用：见 :func:`mine_batch` 与 :func:`infer_term`。
        """

        await self._extract_pass()
        await self._infer_pass()

    async def _extract_pass(self) -> None:
        """对每个开了学习开关的会话处理满批语料。

        :return: ``None``。
        :raises sqlite3.Error: 枚举或游标读写失败时抛出。
        副作用：推进各会话游标；写 ``jargon`` 表。
        """

        budget = EXTRACTION_CALLS_PER_TICK
        rows = self._db.execute(
            "SELECT DISTINCT stream_id FROM messages WHERE role = 'user' ORDER BY stream_id",
        ).fetchall()
        for row in rows:
            stream_id = int(row['stream_id'])
            if not jargon_learn_enabled(self._db, stream_id):
                continue
            while budget > 0:
                cursor = read_cursor(self._store, stream_id)
                pending = self._store.message_count_after(stream_id, cursor)
                if pending < MIN_BATCH_MESSAGES:
                    break
                batch = self._store.messages_after(stream_id, cursor, MAX_BATCH_MESSAGES)
                if not batch:
                    break
                try:
                    outcome = await mine_batch(
                        self._db,
                        self._provider,
                        stream_id=stream_id,
                        batch=batch,
                        bot_name=self._bot_name,
                        bot_names=self._bot_names,
                        temperature=self._temperature,
                        max_tokens=self._max_tokens,
                    )
                except Exception as exc:
                    # 单会话单批失败不推进游标（下轮重跑同一批），也不中断
                    # 其他会话的学习；错误带上下文落日志。
                    logger.warning(
                        'jargon_learn_batch_failed',
                        streamId=stream_id, error=str(exc))
                    break
                if outcome.failed:
                    break
                advance_cursor(self._store, stream_id, batch[-1].message_id)
                if outcome.model_called:
                    budget -= 1
                await asyncio.sleep(0)

    async def _infer_pass(self) -> None:
        """对跨过阶梯阈值的词条执行三步推断，按每轮上限节流。

        :return: ``None``。
        :raises sqlite3.Error: 查询推断目标失败时抛出。
        副作用：先降级并锁定撞已知人名的存量词条（见
            :func:`lock_name_collisions`），再写回
            ``status`` / ``meaning`` / ``inferred_at_sightings``。
        """

        # 先把撞名的存量降级锁定，再挑推断目标：锁定手段就是把
        # inferred_at_sightings 推到上限，下面的选取条件自然把它们排除。
        lock_name_collisions(self._db, self._bot_names)
        processed = 0
        targets = select_inference_targets(
            self._db, INFERENCES_PER_TICK + len(self._poisoned))
        for row in targets:
            if processed >= INFERENCES_PER_TICK:
                break
            term_id = int(row['id'])
            if term_id in self._poisoned:
                continue
            try:
                result = await infer_term(
                    self._db,
                    self._provider,
                    row,
                    bot_name=self._bot_name,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
            except Exception as exc:
                logger.warning(
                    'jargon_learn_infer_failed',
                    term=str(row['term']), error=str(exc))
                continue
            if result == 'parse_failed':
                self._poisoned.add(term_id)
            processed += 1
            await asyncio.sleep(0)
