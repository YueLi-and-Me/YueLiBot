"""调用兼容接口生成记忆向量，并为批量回填提供受控并发入口。

只有 ``[vector].enabled`` 为 ``true`` 时，服务层才会调用本模块；请求使用对话
模型的认证信息和基础地址。向量生成异步执行，单批最多处理 96 条文本；调用失败
由上层记录并保留 BM25 召回路径，避免向量服务故障中断对话。
"""

from __future__ import annotations

import asyncio
import json
import struct

import httpx

from src.core.common.logger import get_logger
from src.core.config.schema import ModelCandidate
from src.core.llm_models.router import ModelRouter

logger = get_logger(__name__)

_BATCH = 96


class EmbeddingClient:
    """通过任务级模型路由器批量生成 float32 向量。

    向量维度取第一条候选的 `embedding_dim`；配置加载器必须保证所有候选维度一致，
    以便新旧向量能够在同一索引中比较。
    """

    def __init__(self, router: ModelRouter) -> None:
        """创建向量客户端。

        :param router: embedding 任务的模型路由器，至少需要一个候选。
        :side_effects: 读取候选列表并缓存第一条候选的向量维度，不发起网络请求。
        :raises IndexError: 路由器没有候选时由索引访问暴露错误；正常入口应先检查 `ready`。
        """
        self._router = router
        self._dim = router.candidates[0].embedding_dim

    @property
    def dim(self) -> int:
        """返回当前候选约定的向量维度。

        :return: embedding 维度整数。
        :side_effects: 不执行模型请求。
        """
        return self._dim

    async def embed(self, texts: list[str]) -> list[bytes | None]:
        """按固定批大小生成文本向量，并保留失败项的位置。

        Args:
            texts: 待向量化的文本列表；输入为空时返回空列表。

        Returns:
            与 ``texts`` 等长的列表；成功项为小端 float32 packed 字节串，批量失败项
            为 ``None``。字节布局适用于内积相似度计算。

        Side Effects:
            通过模型路由器发起每批最多 ``96`` 条文本的网络请求；批次异常仅记录日志，
            不阻断其他批次。

        Performance:
            请求按 ``_BATCH`` 分批，内存占用与输入文本数量及向量维度线性相关。
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
        """为单条文本生成小端 float32 packed 向量。

        :param text: 待向量化的文本。
        :return: packed 向量字节串；批量请求失败时返回 `None`。
        :side_effects: 发起一次最多包含一条文本的 embedding 请求。
        """
        results = await self.embed([text])
        return results[0]

    async def _call(self, texts: list[str]) -> list[list[float]]:
        """通过任务路由器请求一批文本的浮点向量。

        :param texts: 待向量化的文本列表。
        :return: 按输入顺序排列的浮点向量列表。
        :raises Exception: provider 网络、HTTP、鉴权或响应结构错误向路由器传播。
        :side_effects: 通过路由器发起一次非流式模型调用。
        """
        async def request(candidate: ModelCandidate) -> list[list[float]]:
            """使用单个候选模型请求 embedding 接口。

            :param candidate: 已解析的模型候选配置。
            :return: 按服务端 `index` 排序的向量列表。
            :raises httpx.HTTPError: HTTP 请求失败或状态码非成功。
            :raises (KeyError, TypeError): 响应缺少预期 `data` 或 `embedding` 字段。
            :side_effects: 建立一次 HTTP 请求，不写入本地存储。
            """
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
    """把浮点向量编码为连续的小端 float32 字节串。

    :param vec: 浮点向量列表。
    :return: `struct` packed 字节串。
    :raises struct.error: 向量包含无法编码为 float32 的值时抛出。
    :side_effects: 不修改输入列表。
    """
    return struct.pack(f'{len(vec)}f', *vec)


def _unpack(buf: bytes, dim: int) -> list[float]:
    """按指定维度从 packed 字节串解码 float32 向量。

    :param buf: 小端 float32 字节串。
    :param dim: 预期浮点元素数量。
    :return: 解码后的浮点列表。
    :raises struct.error: 字节长度与维度不匹配。
    :side_effects: 不修改输入字节串。
    """
    return list(struct.unpack(f'{dim}f', buf))


def cosine(a: bytes, b: bytes, dim: int) -> float:
    """计算两条 packed 向量的内积；输入已 L2 归一化时等于余弦相似度。

    Args:
        a: 小端 float32 packed 的第一条向量。
        b: 小端 float32 packed 的第二条向量。
        dim: 向量维度，必须与两条字节串的长度一致。

    Returns:
        两条向量的内积浮点值。

    Raises:
        struct.error: 任一字节串长度与 ``dim`` 不匹配。
        TypeError: 输入不是字节串或维度不是整数。
    """
    va = _unpack(a, dim)
    vb = _unpack(b, dim)
    dot = sum(x * y for x, y in zip(va, vb))
    return dot


def build_client(router: ModelRouter) -> EmbeddingClient:
    """在 embedding 路由有候选时创建向量客户端。

    :param router: embedding 任务路由器。
    :return: 新的 :class:`EmbeddingClient`。
    :raises ValueError: 路由没有可用模型候选。
    :side_effects: 只读取路由配置，不发起网络请求。
    """
    if not router.ready:
        raise ValueError('model_tasks.embedding.model_list 是空的，无法启用向量召回')
    return EmbeddingClient(router)
