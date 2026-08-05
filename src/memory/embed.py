"""
Embedding 客户端。

走 OpenAI 兼容的 POST /embeddings，默认复用对话的 Key 和 Base URL。
只有 features.toml 里 [vector].enabled = true 时才会被调用；否则整个模块不执行任何网络请求。

设计约束：
  · 异步，不阻塞事件循环
  · 出错静默降级 —— embedding 失败只是让召回退回纯 BM25，不影响对话
  · 批量写入时分批调用，单批不超过 96 条（避免超 token 限制）
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any

import httpx

from yueli.common.logger import get_logger

logger = get_logger(__name__)

_BATCH = 96


class EmbeddingClient:
    def __init__(self, base_url: str, api_key: str, model: str, dim: int) -> None:
        self._base_url = base_url.rstrip('/')
        self._api_key = api_key
        self._model = model
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[bytes | None]:
        """
        返回与 texts 等长的 bytes 列表；失败项为 None。

        bytes 格式：小端 float32 packed，与 FAISS IndexFlatIP 兼容。
        """
        results: list[bytes | None] = [None] * len(texts)
        for start in range(0, len(texts), _BATCH):
            batch = texts[start:start + _BATCH]
            try:
                vecs = await self._call(batch)
                for i, vec in enumerate(vecs):
                    results[start + i] = _pack(vec)
            except Exception as exc:
                logger.warning("embed_batch_failed", start=start, error=str(exc))
        return results

    async def embed_one(self, text: str) -> bytes | None:
        results = await self.embed([text])
        return results[0]

    async def _call(self, texts: list[str]) -> list[list[float]]:
        headers = {'Content-Type': 'application/json'}
        if self._api_key:
            headers['Authorization'] = f'Bearer {self._api_key}'
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f'{self._base_url}/embeddings',
                headers=headers,
                json={'input': texts, 'model': self._model},
            )
            resp.raise_for_status()
            data = resp.json()
        items = sorted(data['data'], key=lambda x: x['index'])
        return [item['embedding'] for item in items]


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f'{len(vec)}f', *vec)


def _unpack(buf: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f'{dim}f', buf))


def cosine(a: bytes, b: bytes, dim: int) -> float:
    """内积打分（向量已 L2 归一化时等于余弦相似度）。"""
    va = _unpack(a, dim)
    vb = _unpack(b, dim)
    dot = sum(x * y for x, y in zip(va, vb))
    return dot


def build_client_from_config(cfg: Any) -> EmbeddingClient:
    base_url = cfg.vector.embedding_base_url or cfg.llm.base_url
    api_key = cfg.vector.embedding_api_key or cfg.llm.api_key
    if not base_url:
        raise ValueError('vector.embedding_base_url 和 llm.base_url 都没有配置')
    return EmbeddingClient(base_url, api_key, cfg.vector.embedding_model, cfg.vector.embedding_dim)
