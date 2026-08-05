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

import httpx

from src.common.logger import get_logger
from src.config.schema import ModelCandidate
from src.llm_models.router import ModelRouter

logger = get_logger(__name__)

_BATCH = 96


class EmbeddingClient:
    """向量化客户端。候选之间轮询，某个厂商挂了自动换下一条连接。

    ★ 维度取第一条候选的 embedding_dim。备用模型必须输出同样的维度，
      否则新旧向量没法比——所以 loader 会拒绝维度不一致的候选列表。
    """

    def __init__(self, router: ModelRouter) -> None:
        self._router = router
        self._dim = router.candidates[0].embedding_dim

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
        async def request(candidate: ModelCandidate) -> list[list[float]]:
            headers = {'Content-Type': 'application/json'}
            if candidate.api_key:
                headers['Authorization'] = f'Bearer {candidate.api_key}'
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f'{candidate.base_url.rstrip("/")}/embeddings',
                    headers=headers,
                    json={'input': texts, 'model': candidate.identifier},
                )
                resp.raise_for_status()
                data = resp.json()
            items = sorted(data['data'], key=lambda x: x['index'])
            return [item['embedding'] for item in items]

        return await self._router.run(request)


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


def build_client(router: ModelRouter) -> EmbeddingClient:
    if not router.ready:
        raise ValueError('model_tasks.embedding.model_list 是空的，无法启用向量召回')
    return EmbeddingClient(router)
