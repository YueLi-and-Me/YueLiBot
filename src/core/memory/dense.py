"""记忆检索的 SQ8 读侧：批量解码、余弦打分与稠密召回通道。

检索原先只有一条候选来源：FTS5 BM25 取前 ``limit * 3`` 条，向量只给这批候选
重排。语义相近而用词不同的条目进不了候选池，向量无从发挥作用。本模块补上
第二条来源：在 SQ8 向量上按余弦取前 k 条，与 BM25 候选取并集后按同一个融合
公式统一排序。事实（``store.py``）与知识（``knowledge.py``）共用这里的实现。

对外接口：

- :func:`query_unit_vector`：把 float32 packed 查询向量解成单位向量；
- :class:`Q8Matrix` / :func:`q8_matrix`：把一批 ``(id, SQ8 blob)`` 拼成 int8 矩阵；
- :func:`cosine_scores` / :func:`top_k`：整块余弦打分与取前 k；
- :func:`q8_cosine`：单条 SQ8 与查询向量的余弦，供 BM25 候选重排；
- :func:`fused_relevance`：词面与语义相关度的融合公式；
- :class:`KnowledgeVectorIndex`：知识向量的进程内常驻矩阵，按库内修订号失效。

SQ8 格式头的定义与校验来自 ``quantize.py``。余弦直接在 int8 载荷上计算：每条
向量的 scale 是正标量，余弦对正标量缩放不变，乘回 scale 与否结果相同，因此
不做反量化。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

import sqlite3
import time

import numpy as np

from src.core.logging.logger import get_logger

from .quantize import HEADER_BYTES, q8_header_dimension

logger = get_logger(__name__)

# 余弦打分时每块升为 float32 的行数。int8 矩阵整块升精度会临时占用四倍内存
# （2.3 万条 1024 维约 93 MB）；分块后峰值约为 块行数 × 维度 × 4 字节（约 16 MB）。
_SCORE_CHUNK_ROWS = 4096
# 融合权重：词面相关度取 relevance_from_bm25 的 [0, 1) 值，语义相关度取余弦
# 从 [-1, 1] 线性映射到 [0, 1] 的值，两者同一分数域后按 0.4 / 0.6 相加。
_LEXICAL_WEIGHT = 0.4
_SEMANTIC_WEIGHT = 0.6
# meta 表中知识向量修订号的键。schema.py 的触发器在任一行 SQ8 写入、改写或
# 随行删除时把它加一；常驻索引据此判断是否需要重建。
KNOWLEDGE_REVISION_KEY = 'knowledge_vector_revision'


def fused_relevance(lexical: float, cosine: float) -> float:
    """按 0.4 / 0.6 融合词面与语义相关度。

    :param lexical: 词面相关度，取值 ``[0, 1)``；稠密通道独有且词面不匹配的
        条目取 0。
    :param cosine: 查询向量与条目向量的余弦，取值 ``[-1, 1]``。
    :return: ``[0, 1)`` 内的融合相关度，尚未乘留存度权重。
    副作用：无。
    """
    return _LEXICAL_WEIGHT * lexical + _SEMANTIC_WEIGHT * ((cosine + 1) / 2)


def query_unit_vector(query_embedding: bytes) -> np.ndarray:
    """把查询文本的 float32 packed 向量解为 L2 单位向量。

    :param query_embedding: 小端 float32 packed 字节串，维度由长度推导。
    :return: 一维 ``float32`` 单位向量。
    :raises ValueError: 字节长度不是 4 的正整数倍、含非有限值或全为零。
    副作用：无。
    """
    if not query_embedding or len(query_embedding) % 4:
        raise ValueError('查询向量字节长度必须是 4 的正整数倍')
    vector = np.frombuffer(query_embedding, dtype='<f4').astype(np.float32)
    if not np.isfinite(vector).all():
        raise ValueError('查询向量包含非有限值')
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        raise ValueError('查询向量全为零，无法计算余弦')
    return vector / norm


@dataclass(frozen=True)
class Q8Matrix:
    """一批 SQ8 向量的 int8 矩阵形态。

    :ivar ids: 与矩阵行一一对应的业务主键，``int64``，顺序与输入一致。
    :ivar values: ``int8`` 矩阵，形状 ``(行数, 维度)``；空矩阵为 ``(0, 0)``。
    :ivar norms: 每行 int8 载荷的 L2 范数，``float32``；全零向量为 0。
    :ivar dimension: 向量维度；空矩阵为 0。
    """

    ids: np.ndarray
    values: np.ndarray
    norms: np.ndarray
    dimension: int

    def __len__(self) -> int:
        """返回矩阵行数。"""

        return int(self.ids.shape[0])


def q8_matrix(rows: Sequence[Tuple[int, bytes]]) -> Q8Matrix:
    """把一批 ``(id, SQ8 blob)`` 校验后拼成 int8 矩阵。

    :param rows: 业务主键与 SQ8 blob 的序列；可为空。
    :return: 行顺序与输入一致的 :class:`Q8Matrix`。
    :raises ValueError: 任一 blob 格式头非法、维度互不一致，或载荷含对称量化
        范围外的 ``-128``；报错带出问题行的主键。
    副作用：无。
    性能：格式头逐行用 struct 校验，载荷整块交给 numpy 解释；范数分块计算。
    """
    if not rows:
        return Q8Matrix(
            ids=np.empty(0, dtype=np.int64),
            values=np.empty((0, 0), dtype=np.int8),
            norms=np.empty(0, dtype=np.float32),
            dimension=0,
        )
    dimension = 0
    for row_id, blob in rows:
        try:
            row_dimension = q8_header_dimension(blob)
        except ValueError as exc:
            raise ValueError(f'第 {row_id} 行的 SQ8 向量无效：{exc}') from exc
        if dimension == 0:
            dimension = row_dimension
        elif row_dimension != dimension:
            raise ValueError(
                f'SQ8 向量维度不一致：第 {row_id} 行为 {row_dimension}，此前为 {dimension}'
            )
    ids = np.fromiter((int(row_id) for row_id, _ in rows), dtype=np.int64, count=len(rows))
    payload = b''.join(blob[HEADER_BYTES:] for _, blob in rows)
    values = np.frombuffer(payload, dtype=np.int8).reshape(len(rows), dimension)
    out_of_range = np.flatnonzero((values == -128).any(axis=1))
    if out_of_range.size:
        raise ValueError(
            f'第 {int(ids[out_of_range[0]])} 行的 SQ8 载荷包含对称量化范围外的 -128'
        )
    norms = np.empty(len(rows), dtype=np.float32)
    for start in range(0, len(rows), _SCORE_CHUNK_ROWS):
        chunk = values[start:start + _SCORE_CHUNK_ROWS].astype(np.float32)
        norms[start:start + chunk.shape[0]] = np.linalg.norm(chunk, axis=1)
    return Q8Matrix(ids=ids, values=values, norms=norms, dimension=dimension)


def cosine_scores(matrix: Q8Matrix, query: np.ndarray) -> np.ndarray:
    """计算查询单位向量与矩阵每一行的余弦。

    :param matrix: :func:`q8_matrix` 的产物。
    :param query: :func:`query_unit_vector` 的产物。
    :return: 与矩阵行对齐的 ``float32`` 余弦数组，截断在 ``[-1, 1]``；全零
        向量的行记 0。
    :raises ValueError: 查询向量维度与矩阵维度不一致。向量空间登记
        （``vector_space.py``）在换模型时会清空旧向量，正常运行不会出现维度
        不一致；出现即说明库被绕过登记改写过，必须暴露而不是静默跳过。
    副作用：无。
    """
    if len(matrix) == 0:
        return np.empty(0, dtype=np.float32)
    if query.shape[0] != matrix.dimension:
        raise ValueError(
            f'查询向量维度 {query.shape[0]} 与库内 SQ8 向量维度 {matrix.dimension} 不一致'
        )
    dots = np.empty(len(matrix), dtype=np.float32)
    for start in range(0, len(matrix), _SCORE_CHUNK_ROWS):
        chunk = matrix.values[start:start + _SCORE_CHUNK_ROWS].astype(np.float32)
        dots[start:start + chunk.shape[0]] = chunk @ query
    safe_norms = np.where(matrix.norms > 0, matrix.norms, 1.0)
    scores = np.where(matrix.norms > 0, dots / safe_norms, 0.0)
    return np.clip(scores, -1.0, 1.0).astype(np.float32)


def top_k(matrix: Q8Matrix, scores: np.ndarray, k: int) -> List[Tuple[int, float]]:
    """按余弦降序取前 ``k`` 行，余弦相同时按主键升序，保证结果可复现。

    :param matrix: 提供行主键的矩阵。
    :param scores: :func:`cosine_scores` 的产物。
    :param k: 取回条数；小于 1 时返回空列表。
    :return: ``(主键, 余弦)`` 列表，按余弦降序。
    副作用：无。
    """
    if k < 1 or scores.shape[0] == 0:
        return []
    if k < scores.shape[0]:
        picked = np.argpartition(-scores, k - 1)[:k]
    else:
        picked = np.arange(scores.shape[0])
    order = picked[np.lexsort((matrix.ids[picked], -scores[picked]))]
    return [(int(matrix.ids[i]), float(scores[i])) for i in order]


def q8_cosine(query: np.ndarray, blob: bytes) -> float:
    """计算查询单位向量与单条 SQ8 向量的余弦。

    :param query: :func:`query_unit_vector` 的产物。
    :param blob: 带格式头的 SQ8 字节串。
    :return: ``[-1, 1]`` 内的余弦；SQ8 为全零向量时返回 0。
    :raises ValueError: blob 非法、含 ``-128``，或维度与查询向量不一致。
    副作用：无。
    """
    dimension = q8_header_dimension(blob)
    if dimension != query.shape[0]:
        raise ValueError(f'查询向量维度 {query.shape[0]} 与 SQ8 向量维度 {dimension} 不一致')
    values = np.frombuffer(blob, dtype=np.int8, offset=HEADER_BYTES)
    if (values == -128).any():
        raise ValueError('SQ8 载荷包含对称量化范围外的 -128')
    as_float = values.astype(np.float32)
    norm = float(np.linalg.norm(as_float))
    if norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, float(as_float @ query) / norm))


class KnowledgeVectorIndex:
    """知识表 SQ8 向量的进程内常驻矩阵，供稠密召回通道整库打分。

    常驻的原因：知识表每行在 SQ8 列之前还有 4 KB 的 float32 原向量，单行记录
    超出页内容量后，SQLite 读 SQ8 必须顺着溢出页链走完整条记录。真机 2.3 万行
    全量读一次约 0.3～1 秒，逐次查询现读不可接受。常驻矩阵约占 维度 × 行数
    字节（1024 维、2.3 万行约 23 MB）。

    失效判据是 meta 表的修订号（:data:`KNOWLEDGE_REVISION_KEY`）。修订号由
    schema.py 的触发器维护，而不是由本进程的写入函数通知：维护脚本与其他进程
    的写入同样会让它变化。每次查询只多一次主键点查；修订号变化时整表重建，
    真机热缓存约 0.6～1 秒、冷读可达 2.4 秒，重建发生在查询线程上。在线调用方
    （``ConsultAction``）因此经线程池调用，不在事件循环上检索。

    可被多个线程同时使用：缓存是 ``(修订号, 矩阵)`` 单一元组，整体替换是原子的。
    两个线程并发重建时后写者胜出，留下的元组内部始终一致；若拆成两个属性分别
    赋值，交错写入会把旧矩阵标成新修订号，之后再也不会重建。
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        """绑定数据库连接，矩阵在首次查询时构建。

        :param db: 已完成启动期建表的库连接；meta 表须含修订号键。
        """
        self._db = db
        self._cached: Tuple[int, Q8Matrix] | None = None

    def _current_revision(self) -> int:
        """读取 meta 表中的知识向量修订号。

        :return: 当前修订号。
        :raises RuntimeError: 修订号键不存在。该键由 schema.py 在每次启动建表时
            幂等写入，缺失说明数据库没有经过启动期建表，索引无法判断自身是否过期。
        """
        row = self._db.execute(
            'SELECT value FROM meta WHERE key = ?', (KNOWLEDGE_REVISION_KEY,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f'meta 表缺少 {KNOWLEDGE_REVISION_KEY}：数据库未经启动期建表，'
                '知识向量索引无法判断是否过期'
            )
        return int(row[0])

    def matrix(self) -> Q8Matrix:
        """返回与库内当前修订号一致的知识向量矩阵，必要时重建。

        先读修订号再读向量：两次读取之间若有写入，矩阵会比记下的修订号更新，
        下一次查询只会多重建一次；反过来的顺序会让矩阵比修订号旧且不再重建。

        :return: 行按 ``knowledge.id`` 升序排列的 :class:`Q8Matrix`。
        :raises RuntimeError: meta 表缺少修订号键。
        :raises ValueError: 库内存在非法或维度不一致的 SQ8 向量。
        :raises sqlite3.Error: 读取失败。
        副作用：修订号变化时整表读取 ``knowledge.embedding_q8`` 并记一条日志。
        """
        revision = self._current_revision()
        cached = self._cached
        if cached is not None and cached[0] == revision:
            return cached[1]
        started = time.perf_counter()
        rows = self._db.execute(
            '''SELECT id, embedding_q8 FROM knowledge
               WHERE embedding_q8 IS NOT NULL ORDER BY id'''
        ).fetchall()
        matrix = q8_matrix([(int(row[0]), row[1]) for row in rows])
        self._cached = (revision, matrix)
        logger.info(
            'knowledge_vector_index_built',
            rows=len(matrix),
            dim=matrix.dimension,
            revision=revision,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return matrix
