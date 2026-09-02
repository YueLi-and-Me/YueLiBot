"""把 float32 记忆向量编码为可独立解码的对称 int8（SQ8）格式。

每条量化 blob 的布局固定为：

``魔数与版本(4 B) | 维度(uint32) | scale(float32) | int8[维度]``

scale 使用该向量绝对值最大分量除以 127。维度和 scale 都随单条 blob 保存，
读取不依赖模型配置、外部表或写死维度；原 float32 列由迁移保留，便于校验和回滚。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

import asyncio
import math
import sqlite3
import struct

from src.core.common.logger import get_logger

logger = get_logger(__name__)

_MAGIC = b'YQ8\x01'
_HEADER = struct.Struct('<4sIf')
_FLOAT32_BYTES = 4
_INT8_MAX = 127
DEFAULT_BATCH_SIZE = 256


@dataclass
class QuantizationReport:
    """一次存量补算写入的两类行数。"""

    facts: int = 0
    knowledge: int = 0

    @property
    def total(self) -> int:
        """返回两张表本次写入量之和。"""

        return self.facts + self.knowledge


def _unpack_float32(vector: bytes) -> Tuple[float, ...]:
    """从原始列解码一条非空、有限值的小端 float32 向量。"""

    if not vector or len(vector) % _FLOAT32_BYTES != 0:
        raise ValueError('float32 向量字节长度必须是 4 的正整数倍')
    dimension = len(vector) // _FLOAT32_BYTES
    values = struct.unpack(f'<{dimension}f', vector)
    if any(not math.isfinite(value) for value in values):
        raise ValueError('float32 向量包含非有限值')
    return values


def quantize(vector: bytes) -> bytes:
    """把一条 float32 packed 向量编码为自解释 SQ8 blob。

    :param vector: 连续小端 float32 字节串，维度由字节长度推导。
    :return: 带格式版本、维度和单向量 scale 的 SQ8 blob。
    :raises ValueError: 输入为空、长度错误或包含非有限值。
    副作用：无。
    """

    values = _unpack_float32(vector)
    maximum = max(abs(value) for value in values)
    # 全零向量没有自然 scale；使用 1 保持自解释格式有效，载荷仍全部为零。
    scale = maximum / _INT8_MAX if maximum > 0 else 1.0
    payload = [
        max(-_INT8_MAX, min(_INT8_MAX, int(round(value / scale))))
        for value in values
    ]
    return _HEADER.pack(_MAGIC, len(values), scale) + struct.pack(
        f'<{len(payload)}b',
        *payload,
    )


def _decode_q8(blob: bytes) -> Tuple[int, float, Tuple[int, ...]]:
    """校验并解码 SQ8 头部与有符号载荷。"""

    if len(blob) < _HEADER.size:
        raise ValueError('SQ8 blob 短于格式头')
    magic, dimension, scale = _HEADER.unpack_from(blob)
    if magic != _MAGIC:
        raise ValueError('SQ8 blob 的格式标识或版本不受支持')
    if dimension < 1:
        raise ValueError('SQ8 blob 的维度必须大于零')
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError('SQ8 blob 的 scale 必须是有限正数')
    payload = blob[_HEADER.size:]
    if len(payload) != dimension:
        raise ValueError(
            f'SQ8 blob 载荷长度 {len(payload)} 与声明维度 {dimension} 不一致'
        )
    values = struct.unpack(f'<{dimension}b', payload)
    if any(value < -_INT8_MAX for value in values):
        raise ValueError('SQ8 blob 包含对称量化范围外的 -128')
    return dimension, scale, values


def dequantize(blob: bytes) -> bytes:
    """把自解释 SQ8 blob 还原为小端 float32 packed 字节串。"""

    dimension, scale, values = _decode_q8(blob)
    restored = [value * scale for value in values]
    return struct.pack(f'<{dimension}f', *restored)


def cosine_q8(a: bytes, b: bytes) -> float:
    """计算两条 SQ8 向量的余弦相似度。

    :param a: 第一条自解释 SQ8 blob。
    :param b: 第二条自解释 SQ8 blob。
    :return: 量化后向量的余弦相似度；任一向量全零时返回 ``0.0``。
    :raises ValueError: blob 无效或两条向量维度不同。
    副作用：无。
    """

    dimension_a, _, values_a = _decode_q8(a)
    dimension_b, _, values_b = _decode_q8(b)
    if dimension_a != dimension_b:
        raise ValueError(f'SQ8 向量维度不一致：{dimension_a} != {dimension_b}')
    dot = sum(left * right for left, right in zip(values_a, values_b))
    norm_a = sum(value * value for value in values_a)
    norm_b = sum(value * value for value in values_b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    similarity = dot / math.sqrt(norm_a * norm_b)
    return max(-1.0, min(1.0, similarity))


def pending_quantization_counts(db: sqlite3.Connection) -> Dict[str, int]:
    """返回两张表中已有原向量但尚无 SQ8 的行数。"""

    facts = db.execute(
        '''SELECT COUNT(*) FROM facts
           WHERE embedding IS NOT NULL AND embedding_q8 IS NULL'''
    ).fetchone()[0]
    knowledge = db.execute(
        '''SELECT COUNT(*) FROM knowledge
           WHERE embedding IS NOT NULL AND embedding_q8 IS NULL'''
    ).fetchone()[0]
    return {'facts': int(facts), 'knowledge': int(knowledge)}


def store_fact_quantized(
    db: sqlite3.Connection,
    fact_id: int,
    embedding: bytes,
) -> None:
    """由刚生成的原始事实向量同步写入 SQ8 列。"""

    encoded = quantize(embedding)
    db.execute('UPDATE facts SET embedding_q8 = ? WHERE id = ?', (encoded, fact_id))
    db.commit()


def store_knowledge_quantized(
    db: sqlite3.Connection,
    knowledge_id: int,
    embedding: bytes,
) -> None:
    """由刚生成的原始知识向量同步写入 SQ8 列。"""

    encoded = quantize(embedding)
    db.execute(
        'UPDATE knowledge SET embedding_q8 = ? WHERE id = ?',
        (encoded, knowledge_id),
    )
    db.commit()


def store_knowledge_vector_pair(
    db: sqlite3.Connection,
    knowledge_id: int,
    embedding: bytes,
) -> None:
    """用一个 SQL 语句同步写入知识的原向量与 SQ8。

    :param db: 已迁移到包含 ``knowledge.embedding_q8`` 的数据库连接。
    :param knowledge_id: ``knowledge.id`` 稳定主键。
    :param embedding: 小端 float32 packed 向量；维度由字节长度推导。
    :raises ValueError: 原向量不能编码为合法 SQ8 时抛出，数据库保持不变。
    :raises sqlite3.Error: 更新或提交失败时抛出并回滚。
    副作用：原向量与量化向量在同一行、同一条 UPDATE 中提交，避免覆盖错位。
    """

    encoded = quantize(embedding)
    try:
        db.execute(
            '''UPDATE knowledge
               SET embedding = ?, embedding_q8 = ?
               WHERE id = ?''',
            (embedding, encoded, knowledge_id),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


async def _backfill_target(
    db: sqlite3.Connection,
    target: str,
    batch_size: int,
) -> int:
    """按固定目标表补算 SQ8；目标只由本模块内部传入。"""

    total = 0
    while True:
        if target == 'facts':
            rows = db.execute(
                '''SELECT id, embedding FROM facts
                   WHERE embedding IS NOT NULL AND embedding_q8 IS NULL
                   ORDER BY id LIMIT ?''',
                (batch_size,),
            ).fetchall()
        elif target == 'knowledge':
            rows = db.execute(
                '''SELECT id, embedding FROM knowledge
                   WHERE embedding IS NOT NULL AND embedding_q8 IS NULL
                   ORDER BY id LIMIT ?''',
                (batch_size,),
            ).fetchall()
        else:
            raise ValueError(f'不支持的 SQ8 补算目标：{target}')
        if not rows:
            return total
        try:
            for row_id, embedding in rows:
                encoded = quantize(embedding)
                if target == 'facts':
                    db.execute(
                        'UPDATE facts SET embedding_q8 = ? WHERE id = ?',
                        (encoded, int(row_id)),
                    )
                else:
                    db.execute(
                        'UPDATE knowledge SET embedding_q8 = ? WHERE id = ?',
                        (encoded, int(row_id)),
                    )
            db.commit()
        except Exception:
            db.rollback()
            raise
        total += len(rows)
        await asyncio.sleep(0)


async def backfill_quantized_embeddings(
    db: sqlite3.Connection,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> QuantizationReport:
    """为两张表中已有原向量且量化列为空的行补算 SQ8。

    :param db: 已迁移到包含 ``embedding_q8`` 两列的数据库连接。
    :param batch_size: 每次事务处理的行数，必须大于零。
    :return: 分表写入计数。
    :raises ValueError: 批大小无效或遇到坏向量。
    :raises sqlite3.Error: 查询或写入失败。
    副作用：分批更新两张表的 ``embedding_q8``，原 ``embedding`` 不改写。
    """

    if batch_size < 1:
        raise ValueError('SQ8 补算批大小必须大于零')
    facts = await _backfill_target(db, 'facts', batch_size)
    knowledge = await _backfill_target(db, 'knowledge', batch_size)
    report = QuantizationReport(facts=facts, knowledge=knowledge)
    if report.total > 0:
        logger.info(
            'vector_quantize_done',
            facts=report.facts,
            knowledge=report.knowledge,
            total=report.total,
        )
    return report
