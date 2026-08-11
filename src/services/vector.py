"""协调事实向量生成、历史数据补算和查询向量计算。

``VectorService`` 依赖内存存储提供事实查询和向量持久化，依赖可选的嵌入客户端
执行模型调用。未配置客户端时服务保持禁用；嵌入调用失败只影响对应向量操作，
不会阻断事实写入或查询调用方的关键词召回路径。
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.common.logger import get_logger

logger = get_logger(__name__)


class VectorService:
    """管理可选的事实向量生成与批量补算任务。"""

    def __init__(self, store: Any, embed_client: Any | None) -> None:
        """初始化向量服务。

        Args:
            store: 提供 ``store_embedding`` 和 ``facts_without_embedding`` 方法的
                事实存储。
            embed_client: 提供 ``embed_one`` 与 ``embed`` 异步方法的嵌入客户端；
                ``None`` 表示向量功能被配置禁用。
        """

        self._store = store
        self._client = embed_client
        self._enabled = embed_client is not None

    @property
    def enabled(self) -> bool:
        """返回当前是否配置了可用的嵌入客户端。

        Returns:
            客户端不为 ``None`` 时返回 ``True``，否则返回 ``False``。
        """

        return self._enabled

    async def embed_query(self, text: str) -> bytes | None:
        """为查询文本计算实时 embedding。

        Args:
            text: 待向量化的查询文本。

        Returns:
            嵌入客户端返回的序列化向量；服务禁用或调用失败时返回 ``None``，
            调用方可继续使用关键词召回。

        Performance:
            查询结果不缓存，以避免高基数查询占用常驻内存。
        """
        if not self._enabled or not self._client:
            return None
        try:
            return await self._client.embed_one(text)
        except Exception as exc:
            logger.debug("embed_query_failed", error=str(exc))
            return None

    async def embed_fact(self, fact_id: int, content: str) -> None:
        """为一条事实计算并持久化 embedding。

        Args:
            fact_id: ``facts.id`` 稳定主键。
            content: 事实正文。

        Side Effects:
            成功生成向量时更新事实存储；服务禁用或生成失败时记录调试日志并
            保留事实原文，不向调用方抛出嵌入异常。
        """
        if not self._enabled or not self._client:
            return
        try:
            vec = await self._client.embed_one(content)
            if vec is not None:
                self._store.store_embedding(fact_id, vec)
        except Exception as exc:
            logger.debug("embed_fact_failed", id=fact_id, error=str(exc))

    async def backfill(self) -> int:
        """分批补算历史事实中缺失的 embedding。

        Returns:
            本次成功写入向量的事实数量；服务禁用时返回 0。

        Side Effects:
            按每批 32 条读取缺失事实并更新存储；批次之间让出事件循环，避免长期
            占用调度器。

        Raises:
            Exception: 嵌入客户端的批量调用或存储写入异常会直接传播，便于启动期
                发现数据或配置问题。
        """
        if not self._enabled or not self._client:
            return 0
        total = 0
        while True:
            batch = self._store.facts_without_embedding(limit=32)
            if not batch:
                break
            contents = [r['content'] for r in batch]
            ids = [r['id'] for r in batch]
            vecs = await self._client.embed(contents)
            for fact_id, vec in zip(ids, vecs):
                if vec is not None:
                    self._store.store_embedding(fact_id, vec)
                    total += 1
            # 批次之间主动让出事件循环，避免历史补算阻塞在线请求。
            await asyncio.sleep(0)
            if len(batch) < 32:
                break
        if total > 0:
            logger.info("backfill_done", count=total)
        return total
