"""调用兼容接口生成记忆向量，并为批量回填提供受控并发入口。

事实与知识在 ``[vector].enabled`` 时使用本客户端，表情包按任务槽独立装配。
协议请求由模型层执行；本模块仅负责编排批次、编码向量和记录失败。
批次失败保留 None，供调用方继续使用既有关键词或标签匹配路径。
"""

from __future__ import annotations

from typing import List

import struct

from src.core.logging.logger import get_logger
from src.core.config.schema import ModelCandidate
from src.core.llm_models.embeddings import EmbedInput, _BATCH, request_embeddings
from src.core.llm_models.router import ModelRouter

logger = get_logger(__name__)


class EmbeddingClient:
    """通过任务级模型路由器批量生成 float32 向量。

    向量维度取第一条候选的 `embedding_dim`；配置加载器必须保证所有候选维度一致，
    以便新旧向量能够在同一索引中比较。
    """

    def __init__(self, router: ModelRouter) -> None:
        """创建向量客户端。

        :param router: embedding 任务的模型路由器，至少需要一个候选。
        副作用：读取候选列表并缓存第一条候选的向量维度，不发起网络请求。
        :raises IndexError: 路由器没有候选时由索引访问暴露错误；正常入口应先检查 `ready`。
        """
        self._router = router
        self._dim = router.candidates[0].embedding_dim

    @property
    def dim(self) -> int:
        """返回当前候选约定的向量维度。

        :return: embedding 维度整数。
        副作用：不执行模型请求。
        """
        return self._dim

    @property
    def accepts_images(self) -> bool:
        """当前向量空间的协议是否接受已预处理图片。"""
        return self._router.candidates[0].api_format in ('dashscope_multimodal', 'ark_multimodal')

    async def embed(self, texts: List[str]) -> List[bytes | None]:
        """文本调用方保持原接口，按纯文本条目进入同一向量协议。"""
        return await self.embed_inputs([EmbedInput(text=text) for text in texts])

    async def embed_inputs(self, items: List[EmbedInput]) -> List[bytes | None]:
        """按固定批大小生成输入向量，并保留失败项的位置。

        :param items: 文本、图片或融合输入列表；输入为空时返回空列表。

        :return: 与 ``items`` 等长的列表；成功项为小端 float32 packed 字节串，批量失败项
            为 ``None``。字节布局适用于内积相似度计算。

        副作用：
            通过模型路由器发起每批最多 ``_BATCH`` 条输入的网络请求；批次异常仅记录日志，
            不阻断其他批次。

        性能：
            请求按 ``_BATCH`` 分批，内存占用与输入文本数量及向量维度线性相关。
        """
        results: list[bytes | None] = [None] * len(items)
        for start in range(0, len(items), _BATCH):
            batch = items[start:start + _BATCH]
            try:
                vecs = await self._call_inputs(batch)
                for i, vec in enumerate(vecs):
                    results[start + i] = _pack(vec)
            except Exception as exc:
                logger.warning("embed_batch_failed", start=start, error=str(exc))
        return results

    async def embed_one(self, text: str) -> bytes | None:
        """为单条文本生成小端 float32 packed 向量。

        :param text: 待向量化的文本。
        :return: packed 向量字节串；批量请求失败时返回 `None`。
        副作用：发起一次最多包含一条文本的 embedding 请求。
        """
        results = await self.embed([text])
        return results[0]

    async def _call(self, texts: list[str]) -> list[list[float]]:
        """通过任务路由器请求一批文本的浮点向量。

        :param texts: 待向量化的文本列表。
        :return: 按输入顺序排列的浮点向量列表。
        :raises Exception: provider 网络、HTTP、鉴权或响应结构错误向路由器传播。
        副作用：通过路由器发起一次非流式模型调用。
        """
        return await self._call_inputs([EmbedInput(text=text) for text in texts])

    async def _call_inputs(self, items: List[EmbedInput]) -> List[List[float]]:
        """让路由在同一向量空间的候选之间切换；失败完整传播供批次日志记录。"""
        async def request(candidate: ModelCandidate) -> List[List[float]]:
            return await request_embeddings(candidate, items)

        return await self._router.run(request)


def _pack(vec: list[float]) -> bytes:
    """把浮点向量编码为连续的小端 float32 字节串。

    :param vec: 浮点向量列表。
    :return: `struct` packed 字节串。
    :raises struct.error: 向量包含无法编码为 float32 的值时抛出。
    副作用：不修改输入列表。
    """
    return struct.pack(f'{len(vec)}f', *vec)


def _unpack(buf: bytes, dim: int) -> list[float]:
    """按指定维度从 packed 字节串解码 float32 向量。

    :param buf: 小端 float32 字节串。
    :param dim: 预期浮点元素数量。
    :return: 解码后的浮点列表。
    :raises struct.error: 字节长度与维度不匹配。
    副作用：不修改输入字节串。
    """
    return list(struct.unpack(f'{dim}f', buf))


def cosine(a: bytes, b: bytes, dim: int) -> float:
    """计算两条 packed 向量的内积；输入已 L2 归一化时等于余弦相似度。

    :param a: 小端 float32 packed 的第一条向量。
    :param b: 小端 float32 packed 的第二条向量。
    :param dim: 向量维度，必须与两条字节串的长度一致。

    :return: 两条向量的内积浮点值。

    :raises struct.error: 任一字节串长度与 ``dim`` 不匹配。
    :raises TypeError: 输入不是字节串或维度不是整数。
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
    副作用：只读取路由配置，不发起网络请求。
    """
    if not router.ready:
        raise ValueError('model_tasks.embedding.model_list 是空的，无法启用向量召回')
    return EmbeddingClient(router)
