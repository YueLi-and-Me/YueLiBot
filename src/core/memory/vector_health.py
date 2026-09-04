"""只读汇总事实与知识向量列的覆盖和格式健康状态。

启动期的待补算告警与主动自检都从这里取数，避免一个按 ``NULL`` 计数、另一个
按业务对象列表计数而逐渐漂移。模块只执行 ``SELECT``，不会触发补算或修复。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Set, Tuple

import sqlite3

from .quantize import embedding_dimension, quantized_dimension

_PROBLEM_SAMPLE_LIMIT = 5


@dataclass(frozen=True)
class VectorTableHealth:
    """一张记忆表的向量覆盖、维度和逐行一致性快照。"""

    table: str
    total: int
    embedding_count: int
    quantized_count: int
    embedding_without_quantized: int
    quantized_without_embedding: int
    invalid_embedding_count: int
    invalid_quantized_count: int
    dimension_mismatch_count: int
    embedding_dimensions: Tuple[int, ...]
    quantized_dimensions: Tuple[int, ...]
    problem_samples: Tuple[str, ...]

    @property
    def missing_embedding_count(self) -> int:
        """返回原始 embedding 为空的行数。"""
        return self.total - self.embedding_count

    @property
    def missing_quantized_count(self) -> int:
        """返回 SQ8 为空的行数。"""
        return self.total - self.quantized_count


@dataclass(frozen=True)
class VectorHealth:
    """事实与知识两张表在同一只读快照中的向量健康状态。"""

    facts: VectorTableHealth
    knowledge: VectorTableHealth


def pending_embedding_counts(db: sqlite3.Connection) -> Dict[str, int]:
    """返回事实与知识表中缺少原始 embedding 的精确行数。

    :param db: 已打开且包含当前记忆表结构的 SQLite 连接。
    :return: ``facts`` / ``knowledge`` 到待补算条数的字典。
    :raises sqlite3.Error: 表或字段缺失、查询失败时传播原始异常。
    副作用：只读两张表。
    """
    facts = db.execute(
        'SELECT COUNT(*) FROM facts WHERE embedding IS NULL'
    ).fetchone()[0]
    knowledge = db.execute(
        'SELECT COUNT(*) FROM knowledge WHERE embedding IS NULL'
    ).fetchone()[0]
    return {'facts': int(facts), 'knowledge': int(knowledge)}


def inspect_vector_health(db: sqlite3.Connection) -> VectorHealth:
    """在当前 SQLite 快照中校验两张向量表。

    :param db: 已打开且包含当前记忆表结构的 SQLite 连接。
    :return: 覆盖率、逐行空值关系、格式与维度快照。
    :raises sqlite3.Error: 表或字段缺失、查询失败时传播原始异常。
    副作用：只读 ``facts`` 与 ``knowledge``。
    """
    fact_rows = db.execute(
        'SELECT id, embedding, embedding_q8 FROM facts ORDER BY id'
    ).fetchall()
    knowledge_rows = db.execute(
        'SELECT id, embedding, embedding_q8 FROM knowledge ORDER BY id'
    ).fetchall()
    return VectorHealth(
        facts=_inspect_table('facts', fact_rows),
        knowledge=_inspect_table('knowledge', knowledge_rows),
    )


def _inspect_table(
    table: str,
    rows: Sequence[Sequence[Any]],
) -> VectorTableHealth:
    """校验一张已由固定 SQL 选出的向量表。"""
    embedding_count = 0
    quantized_count = 0
    embedding_without_quantized = 0
    quantized_without_embedding = 0
    invalid_embedding_count = 0
    invalid_quantized_count = 0
    dimension_mismatch_count = 0
    embedding_dimensions: Set[int] = set()
    quantized_dimensions: Set[int] = set()
    problems: List[str] = []

    for row in rows:
        row_id = int(row[0])
        embedding = row[1]
        quantized = row[2]
        embedding_dim: int | None = None
        quantized_dim: int | None = None

        if embedding is not None:
            embedding_count += 1
            try:
                embedding_dim = embedding_dimension(_as_blob(embedding))
                embedding_dimensions.add(embedding_dim)
            except (TypeError, ValueError) as exc:
                invalid_embedding_count += 1
                _append_problem(problems, f'{table}.id={row_id} 原始向量非法：{exc}')
        if quantized is not None:
            quantized_count += 1
            try:
                quantized_dim = quantized_dimension(_as_blob(quantized))
                quantized_dimensions.add(quantized_dim)
            except (TypeError, ValueError) as exc:
                invalid_quantized_count += 1
                _append_problem(problems, f'{table}.id={row_id} SQ8 非法：{exc}')

        if embedding is not None and quantized is None:
            embedding_without_quantized += 1
            _append_problem(problems, f'{table}.id={row_id} 有原始向量但没有 SQ8')
        elif embedding is None and quantized is not None:
            quantized_without_embedding += 1
            _append_problem(problems, f'{table}.id={row_id} 有 SQ8 但没有原始向量')
        elif (
            embedding_dim is not None
            and quantized_dim is not None
            and embedding_dim != quantized_dim
        ):
            dimension_mismatch_count += 1
            _append_problem(
                problems,
                f'{table}.id={row_id} 原始维度 {embedding_dim} 与 SQ8 维度 '
                f'{quantized_dim} 不一致',
            )

    return VectorTableHealth(
        table=table,
        total=len(rows),
        embedding_count=embedding_count,
        quantized_count=quantized_count,
        embedding_without_quantized=embedding_without_quantized,
        quantized_without_embedding=quantized_without_embedding,
        invalid_embedding_count=invalid_embedding_count,
        invalid_quantized_count=invalid_quantized_count,
        dimension_mismatch_count=dimension_mismatch_count,
        embedding_dimensions=tuple(sorted(embedding_dimensions)),
        quantized_dimensions=tuple(sorted(quantized_dimensions)),
        problem_samples=tuple(problems),
    )


def _append_problem(problems: List[str], message: str) -> None:
    """在固定上限内保存可定位样例，完整数量由独立计数字段保留。"""
    if len(problems) < _PROBLEM_SAMPLE_LIMIT:
        problems.append(message)


def _as_blob(value: Any) -> bytes:
    """只接受 SQLite BLOB 会返回的字节类型，拒绝动态类型污染。"""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f'向量列实际类型为 {type(value).__name__}，不是 BLOB')
    return bytes(value)
