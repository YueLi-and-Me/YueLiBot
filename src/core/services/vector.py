"""协调事实与知识向量生成、历史数据补算和查询向量计算。

``VectorService`` 依赖内存存储和数据库连接持久化向量，依赖可选的嵌入客户端
执行模型调用。未配置客户端时服务保持禁用；嵌入调用失败只影响对应向量操作，
不会阻断事实、知识正文写入或查询调用方的关键词召回路径。
"""

from __future__ import annotations

from typing import Any, Optional

import asyncio
import sqlite3

from src.core.common.logger import get_logger
from src.core.memory.knowledge import knowledge_without_embedding
from src.core.memory.quantize import (
    backfill_quantized_embeddings,
    pending_quantization_counts,
    store_fact_quantized,
    store_knowledge_vector_pair,
)
from src.core.memory.vector_health import pending_embedding_counts

logger = get_logger(__name__)


class VectorService:
    """管理可选的事实、知识向量生成与批量补算任务。"""

    def __init__(
        self,
        store: Any,
        embed_client: Any | None,
        *,
        disabled_reason: str | None = None,
        db: Optional[sqlite3.Connection] = None,
    ) -> None:
        """初始化向量服务。

        :param store: 提供 ``store_embedding`` 和 ``facts_without_embedding`` 方法的
                事实存储。
        :param embed_client: 提供 ``embed_one`` 与 ``embed`` 异步方法的嵌入客户端；
                ``None`` 表示向量功能被配置禁用。
        :param disabled_reason: 客户端未装配时用于启动告警的明确原因。
        :param db: 已迁移到当前结构的数据库连接；提供后同步维护并补算 SQ8 列。
        """

        self._store = store
        self._client = embed_client
        self._disabled_reason = disabled_reason
        self._db = db
        self._backfill_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        """返回当前是否配置了可用的嵌入客户端。

        :return: 客户端不为 ``None`` 时返回 ``True``，否则返回 ``False``。
        """

        return self._client is not None

    async def startup(self) -> None:
        """显式报告装配状态，并把事实与知识补算挂成一次性后台任务。

        :return: ``None``；补算任务创建后立即返回，不等待模型批次完成。
        副作用：
            服务未装配时发一条启动告警；已装配且存在缺失向量的事实或知识时发告警，
            随后创建 ``vector-backfill`` 后台任务。
        """

        if self._client is None:
            logger.warning(
                'vector_service_disabled',
                reason=self._disabled_reason or 'embedding 客户端未装配',
            )
        else:
            if self._db is not None:
                pending = pending_embedding_counts(self._db)
                if pending['facts'] > 0:
                    logger.warning(
                        'vector_fact_backfill_pending',
                        count=pending['facts'],
                    )
                if pending['knowledge'] > 0:
                    logger.warning(
                        'vector_knowledge_backfill_pending',
                        count=pending['knowledge'],
                    )
            else:
                pending_facts = self._store.facts_without_embedding(limit=1_000_000)
                if pending_facts:
                    logger.warning(
                        'vector_fact_backfill_pending',
                        count=len(pending_facts),
                    )
        quantize_total = 0
        if self._db is not None:
            quantize_pending = pending_quantization_counts(self._db)
            quantize_total = quantize_pending['facts'] + quantize_pending['knowledge']
            if quantize_total > 0:
                logger.warning(
                    'vector_quantize_pending',
                    facts=quantize_pending['facts'],
                    knowledge=quantize_pending['knowledge'],
                    total=quantize_total,
                )
        # SQ8 是已有原向量的本地派生数据，不依赖 provider；即使在线向量功能
        # 暂时未装配，也应完成这部分存量补算。两类待办都为空时不创建空任务。
        if self._client is None and quantize_total == 0:
            return
        self._backfill_task = asyncio.create_task(
            self._run_backfill(),
            name='vector-backfill',
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
        try:
            await self.backfill_knowledge()
        except Exception as exc:
            logger.error('vector_knowledge_backfill_failed', error=str(exc))
        if self._db is None:
            return
        try:
            await backfill_quantized_embeddings(self._db)
        except Exception as exc:
            logger.error('vector_quantize_failed', error=str(exc))

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
            if vec is None:
                return
            self._store.store_embedding(fact_id, vec)
        except Exception as exc:
            logger.debug("embed_fact_failed", id=fact_id, error=str(exc))
            return
        if self._db is not None:
            try:
                store_fact_quantized(self._db, fact_id, vec)
            except Exception as exc:
                logger.error('embed_fact_quantize_failed', id=fact_id, error=str(exc))

    async def embed_knowledge(self, knowledge_id: int, content: str) -> None:
        """为一条知识生成并同步持久化原向量与 SQ8。

        :param knowledge_id: ``knowledge.id`` 稳定主键。
        :param content: 知识正文。
        副作用：成功时在一个 SQL 更新里写入 ``embedding`` 与 ``embedding_q8``；
            服务未装配时不重复告警，装配状态已由 :meth:`startup` 统一报告。
        """

        if self._client is None:
            return
        if self._db is None:
            logger.warning(
                'vector_knowledge_write_failed',
                id=knowledge_id,
                reason='数据库连接未装配',
            )
            return
        try:
            vec = await self._client.embed_one(content)
            if vec is None:
                logger.warning(
                    'vector_knowledge_write_failed',
                    id=knowledge_id,
                    reason='embedding 返回空值',
                )
                return
            store_knowledge_vector_pair(self._db, knowledge_id, vec)
        except Exception as exc:
            logger.warning(
                'vector_knowledge_write_failed',
                id=knowledge_id,
                error=str(exc),
            )

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

    async def backfill_knowledge(self) -> int:
        """为启动时缺失原向量的知识生成原向量与 SQ8。

        :return: 本次成功写入两列的知识数量；服务或数据库未装配时返回 0。
        :raises Exception: provider、量化或数据库错误直接传播给启动任务记录。
        副作用：冻结一次待办快照，按每批 32 条请求 provider；成功行以单条
            ``UPDATE`` 同步写入两列，失败行保持两列 ``NULL`` 供下次启动续算。
        """

        if self._client is None or self._db is None:
            return 0
        pending = knowledge_without_embedding(self._db, limit=1_000_000)
        total = 0
        failed = 0
        for start in range(0, len(pending), 32):
            batch = pending[start:start + 32]
            contents = [content for _, content in batch]
            ids = [knowledge_id for knowledge_id, _ in batch]
            vecs = await self._client.embed(contents)
            for knowledge_id, vec in zip(ids, vecs, strict=True):
                if vec is None:
                    failed += 1
                    continue
                store_knowledge_vector_pair(self._db, knowledge_id, vec)
                total += 1
            await asyncio.sleep(0)
        if failed > 0:
            logger.warning('vector_knowledge_backfill_left_null', count=failed)
        if total > 0:
            logger.info('vector_knowledge_backfill_done', count=total)
        return total
