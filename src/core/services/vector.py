"""协调事实向量生成、历史数据补算和查询向量计算。

``VectorService`` 依赖内存存储提供事实查询和向量持久化，依赖可选的嵌入客户端
执行模型调用。未配置客户端时服务保持禁用；嵌入调用失败只影响对应向量操作，
不会阻断事实写入或查询调用方的关键词召回路径。
"""

from __future__ import annotations

from typing import Any

import asyncio

from src.core.common.logger import get_logger

logger = get_logger(__name__)


class VectorService:
    """管理可选的事实向量生成与批量补算任务。"""

    def __init__(
        self,
        store: Any,
        embed_client: Any | None,
        *,
        disabled_reason: str | None = None,
    ) -> None:
        """初始化向量服务。

        :param store: 提供 ``store_embedding`` 和 ``facts_without_embedding`` 方法的
                事实存储。
        :param embed_client: 提供 ``embed_one`` 与 ``embed`` 异步方法的嵌入客户端；
                ``None`` 表示向量功能被配置禁用。
        :param disabled_reason: 客户端未装配时用于启动告警的明确原因。
        """

        self._store = store
        self._client = embed_client
        self._disabled_reason = disabled_reason
        self._backfill_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        """返回当前是否配置了可用的嵌入客户端。

        :return: 客户端不为 ``None`` 时返回 ``True``，否则返回 ``False``。
        """

        return self._client is not None

    async def startup(self) -> None:
        """显式报告装配状态，并把历史事实补算挂成一次性后台任务。

        :return: ``None``；补算任务创建后立即返回，不等待模型批次完成。
        副作用：
            服务未装配时发一条启动告警；已装配且存在缺失向量的事实时发告警，
            随后创建 ``vector-fact-backfill`` 后台任务。
        """

        if self._client is None:
            logger.warning(
                'vector_service_disabled',
                reason=self._disabled_reason or 'embedding 客户端未装配',
            )
            return
        pending = self._store.facts_without_embedding(limit=1_000_000)
        if pending:
            logger.warning('vector_fact_backfill_pending', count=len(pending))
        self._backfill_task = asyncio.create_task(
            self._run_backfill(),
            name='vector-fact-backfill',
        )

    async def shutdown(self) -> None:
        """取消仍在执行的启动期补算；未完成行保留 ``NULL``，下次启动续算。

        :return: ``None``。
        副作用：取消并等待后台任务结束，不修改已经成功写入的向量。
        """

        if self._backfill_task is None:
            return
        if not self._backfill_task.done():
            self._backfill_task.cancel()
        try:
            await self._backfill_task
        except asyncio.CancelledError:
            pass
        self._backfill_task = None

    async def _run_backfill(self) -> None:
        """执行一次补算并把异常留在启动日志中，避免后台任务静默失败。"""

        try:
            await self.backfill()
        except Exception as exc:
            logger.error('vector_backfill_failed', error=str(exc))

    async def embed_query(self, text: str) -> bytes | None:
        """为查询文本计算实时 embedding。

        :param text: 待向量化的查询文本。

        :return: 嵌入客户端返回的序列化向量；服务禁用或调用失败时返回 ``None``，
            调用方可继续使用关键词召回。

        性能：
            查询结果不缓存，以避免高基数查询占用常驻内存。
        """
        if self._client is None:
            return None
        try:
            return await self._client.embed_one(text)
        except Exception as exc:
            logger.debug("embed_query_failed", error=str(exc))
            return None

    async def embed_fact(self, fact_id: int, content: str) -> None:
        """为一条事实计算并持久化 embedding。

        :param fact_id: ``facts.id`` 稳定主键。
        :param content: 事实正文。

        副作用：
            成功生成向量时更新事实存储；服务禁用或生成失败时记录调试日志并
            保留事实原文，不向调用方抛出嵌入异常。
        """
        if self._client is None:
            return
        try:
            vec = await self._client.embed_one(content)
            if vec is not None:
                self._store.store_embedding(fact_id, vec)
        except Exception as exc:
            logger.debug("embed_fact_failed", id=fact_id, error=str(exc))

    async def backfill(self) -> int:
        """分批补算历史事实中缺失的 embedding。

        :return: 本次成功写入向量的事实数量；服务禁用时返回 0。

        副作用：
            按每批 32 条读取缺失事实并更新存储；批次之间让出事件循环，避免长期
            占用调度器。

        :raises Exception: 嵌入客户端的批量调用或存储写入异常会直接传播，便于启动期
                发现数据或配置问题。
        """
        if self._client is None:
            return 0
        # 冻结本次启动看到的待办快照；失败项本次只尝试一次，保持 NULL 供下次
        # 启动续算。循环反复查询 NULL 会在 provider 整批失败时永远选中同一批。
        pending = self._store.facts_without_embedding(limit=1_000_000)
        total = 0
        failed = 0
        for start in range(0, len(pending), 32):
            batch = pending[start:start + 32]
            contents = [r['content'] for r in batch]
            ids = [r['id'] for r in batch]
            vecs = await self._client.embed(contents)
            for fact_id, vec in zip(ids, vecs):
                if vec is not None:
                    self._store.store_embedding(fact_id, vec)
                    total += 1
                else:
                    failed += 1
            # 批次之间主动让出事件循环，避免历史补算阻塞在线请求。
            await asyncio.sleep(0)
        if failed > 0:
            logger.warning('vector_backfill_left_null', count=failed)
        if total > 0:
            logger.info("backfill_done", count=total)
        return total
