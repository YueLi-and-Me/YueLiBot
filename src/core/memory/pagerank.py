"""提供与存储无关的个性化 PageRank 稳态计算。"""

from __future__ import annotations

from math import isfinite
from time import monotonic
from typing import Dict, Hashable, Mapping, TypeVar


NodeT = TypeVar('NodeT', bound=Hashable)


class PageRankTimeoutError(TimeoutError):
    """个性化 PageRank 在时限内未完成。"""


def personalized_pagerank(
    adjacency: Mapping[NodeT, Mapping[NodeT, float]],
    personalization: Mapping[NodeT, float],
    *,
    alpha: float = 0.85,
    max_iterations: int = 100,
    tolerance: float = 1e-9,
    timeout_seconds: float = 0.1,
) -> Dict[NodeT, float]:
    """计算一张加权有向图的个性化 PageRank 稳态分布。

    每个节点的出边在函数内归一化；悬空节点的质量按个性化向量重新分配，
    因而每轮迭代仍保持概率和为一。达到迭代上限但未收敛时返回最后一轮，
    超过墙钟时限则抛出专用异常，由调用层决定是否回退。

    :param adjacency: ``节点 -> {邻居: 非负边权}`` 的邻接表。
    :param personalization: 节点的非负个性化权重；无需预先归一化。
    :param alpha: 沿图游走的阻尼系数，范围为 ``[0, 1)``。
    :param max_iterations: 最大迭代轮数，必须大于零。
    :param tolerance: 相邻两轮 L1 差的收敛阈值，必须大于零。
    :param timeout_seconds: 墙钟超时秒数，必须大于零。
    :return: 覆盖图中全部节点的稳态概率分布；空图返回空字典。
    :raises ValueError: 参数、个性化权重或边权无效。
    :raises PageRankTimeoutError: 计算超过墙钟时限。
    副作用：无。
    """
    if not 0.0 <= alpha < 1.0:
        raise ValueError('PageRank 阻尼系数必须在 [0, 1) 内')
    if max_iterations < 1:
        raise ValueError('PageRank 迭代上限必须大于 0')
    if tolerance <= 0.0 or not isfinite(tolerance):
        raise ValueError('PageRank 收敛阈值必须是有限正数')
    if timeout_seconds <= 0.0 or not isfinite(timeout_seconds):
        raise ValueError('PageRank 超时必须是有限正数')

    # 用字典保留调用方给出的稳定顺序；节点类型无需可排序。
    nodes: Dict[NodeT, None] = {}
    for node, outgoing in adjacency.items():
        nodes[node] = None
        for target in outgoing:
            nodes[target] = None
    for node in personalization:
        nodes[node] = None
    if not nodes:
        return {}

    transitions: Dict[NodeT, Dict[NodeT, float]] = {}
    for node in nodes:
        outgoing = adjacency.get(node, {})
        total = 0.0
        checked: Dict[NodeT, float] = {}
        for target, raw_weight in outgoing.items():
            weight = float(raw_weight)
            if weight < 0.0 or not isfinite(weight):
                raise ValueError('PageRank 边权必须是有限非负数')
            if weight == 0.0:
                continue
            checked[target] = weight
            total += weight
        transitions[node] = (
            {target: weight / total for target, weight in checked.items()}
            if total > 0.0
            else {}
        )

    personal_raw: Dict[NodeT, float] = {}
    personal_total = 0.0
    for node in nodes:
        weight = float(personalization.get(node, 0.0))
        if weight < 0.0 or not isfinite(weight):
            raise ValueError('PageRank 个性化权重必须是有限非负数')
        personal_raw[node] = weight
        personal_total += weight
    if personal_total <= 0.0:
        raise ValueError('PageRank 个性化向量至少要有一个正权重')
    personal = {node: weight / personal_total for node, weight in personal_raw.items()}

    ranks = dict(personal)
    deadline = monotonic() + timeout_seconds
    for _iteration in range(max_iterations):
        if monotonic() >= deadline:
            raise PageRankTimeoutError('个性化 PageRank 计算超时')
        dangling = sum(ranks[node] for node, outgoing in transitions.items() if not outgoing)
        updated = {
            node: ((1.0 - alpha) + alpha * dangling) * personal[node]
            for node in nodes
        }
        for source, outgoing in transitions.items():
            share = alpha * ranks[source]
            for target, probability in outgoing.items():
                updated[target] += share * probability
        if monotonic() >= deadline:
            raise PageRankTimeoutError('个性化 PageRank 计算超时')
        delta = sum(abs(updated[node] - ranks[node]) for node in nodes)
        ranks = updated
        if delta <= tolerance:
            break

    total_rank = sum(ranks.values())
    return {node: value / total_rank for node, value in ranks.items()}


__all__ = ['PageRankTimeoutError', 'personalized_pagerank']
