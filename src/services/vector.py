"""
向量召回服务。

负责：
  · 后台异步批量补算缺失的 embedding（进程启动后触发一次）
  · 为每条新事实在写入后调度 embedding 计算
  · 为查询文本实时计算 embedding，注入 store.recall_facts()

features.toml 里 [vector].enabled = false（默认）时整个服务不运行，所有方法都是 no-op。
"""

from __future__ import annotations

import asyncio
from typing import Any

from yueli.common.logger import get_logger

logger = get_logger(__name__)


class VectorService:
    """
    向量召回服务。

    store: MemoryStore 实例
    embed_client: EmbeddingClient 实例；None 表示已被配置禁用
    """

    def __init__(self, store: Any, embed_client: Any | None) -> None:
        self._store = store
        self._client = embed_client
        self._enabled = embed_client is not None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def embed_query(self, text: str) -> bytes | None:
        """
        为查询文本计算实时 embedding。

        失败时返回 None，调用方据此退回纯 BM25。
        故意不做缓存：查询多样，缓存收益低，还增加内存压力。
        """
        if not self._enabled or not self._client:
            return None
        try:
            return await self._client.embed_one(text)
        except Exception as exc:
            logger.debug("embed_query_failed", error=str(exc))
            return None

    async def embed_fact(self, fact_id: int, content: str) -> None:
        """为一条新事实计算并持久化 embedding。"""
        if not self._enabled or not self._client:
            return
        try:
            vec = await self._client.embed_one(content)
            if vec is not None:
                self._store.store_embedding(fact_id, vec)
        except Exception as exc:
            logger.debug("embed_fact_failed", id=fact_id, error=str(exc))

    async def backfill(self) -> int:
        """
        后台补算所有缺失 embedding 的事实。

        在进程启动后触发一次，确保历史数据也被向量化。
        每批之间 yield 一次以让事件循环有机会处理其他请求。
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
            # yield 让事件循环处理其他请求
            await asyncio.sleep(0)
            if len(batch) < 32:
                break
        if total > 0:
            logger.info("backfill_done", count=total)
        return total
