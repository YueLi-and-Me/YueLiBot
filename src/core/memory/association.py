"""事实、情节与知识三层记忆之间的关联边存储与扩散召回。

``memory_nodes`` 将三层已有记录映射到统一节点 ID；``memory_edges`` 保存节点间的
无向关联边。召回时，统一记忆图与 ``knowledge_edges`` 概念图分别运行 PPR，
只在结果层融合。扩散与建边的中间结果均以结构化数据落账，可在观察面板中回放。

对外暴露 :func:`node_id`、:func:`link_together`、:func:`spread` 与
:class:`ShortTermActivation`。被 ``agent/fact_extract``（写入后建边）与
``agent/cognition``（召回后扩散）使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

import math
import sqlite3

from src.core.observe import events as trace

from .decay import FREEZE, REVIVE, reinforce, retention
from .pagerank import PageRankTimeoutError, personalized_pagerank
from .tuning import ppr_alpha as tuned_ppr_alpha
from .tuning import ppr_hops as tuned_ppr_hops

# 沿边扩散的最大跳数。
HOPS = 2
# PPR 超时时，旧扩散回退算法使用的每跳权重衰减系数。
HOP_DECAY = 0.5
# 单次扩散返回的结果条数上限。
SPREAD_LIMIT = 4
# 建边与加强时的强度增量。
EDGE_BOOST = 0.35
# 边强度的半衰期，单位小时。
EDGE_HALF_LIFE_HOURS = 720.0
# 短期激活的衰减时间常数，单位毫秒。
ACTIVATION_TAU_MS = 10 * 60 * 1000
# PPR 只约束单次纯计算；建图查询不计入墙钟时限。
PPR_ALPHA = 0.85
PPR_MAX_ITERATIONS = 100
PPR_TOLERANCE = 1e-9
PPR_TIMEOUT_SECONDS = 0.1

# ``memory_nodes.ref_kind`` 的合法取值。
REF_KINDS = ('fact', 'episode', 'knowledge')


@dataclass(frozen=True)
class SpreadHit:
    """一次扩散命中的记忆。

    :ivar node_id: ``memory_nodes`` 主键。
    :ivar ref_kind: 来源层，``fact`` / ``episode`` / ``knowledge``。
    :ivar ref_id: 该层内的主键。
    :ivar score: 两张图的 PPR 融合分数，已含邻居留存度与短期激活。
    :ivar hops: 距离最近的种子有几跳；种子本身为 ``0``。
    """

    node_id: int
    ref_kind: str
    ref_id: int
    score: float
    hops: int


def node_id(db: sqlite3.Connection, ref_kind: str, ref_id: int) -> int:
    """取得（必要时登记）一条记忆在联想网络里的节点 ID。

    :param db: 当前库连接。
    :param ref_kind: 来源层，必须是 :data:`REF_KINDS` 之一。
    :param ref_id: 该层内的主键。
    :return: ``memory_nodes`` 主键。
    :raises ValueError: ``ref_kind`` 不在允许集合内。
    :raises sqlite3.Error: 读写失败。
    副作用：可能向 ``memory_nodes`` 插入一行；不提交事务，由调用方统一提交。
    """
    if ref_kind not in REF_KINDS:
        raise ValueError(f'未知的记忆来源层：{ref_kind}')
    row = db.execute(
        'SELECT id FROM memory_nodes WHERE ref_kind = ? AND ref_id = ?', (ref_kind, ref_id)
    ).fetchone()
    if row is not None:
        return int(row[0])
    cursor = db.execute(
        'INSERT INTO memory_nodes (ref_kind, ref_id) VALUES (?, ?)', (ref_kind, ref_id)
    )
    return int(cursor.lastrowid)


def link_together(
    db: sqlite3.Connection,
    refs: Sequence[Tuple[str, int]],
    now: int,
) -> int:
    """对同批共同出现的记忆两两建立或加强关联边。

    建边仅发生在两种场景：同一批写入，或同一次召回中被实际采用并进入提示词。
    调用方必须只传入实际使用的节点；未被使用的检索结果不参与建边。

    强度增长复用 :func:`~src.core.memory.decay.reinforce` 的饱和口径。

    :param db: 当前库连接。
    :param refs: ``(ref_kind, ref_id)`` 序列；少于两条时不建任何边。
    :param now: 当前毫秒时间戳。
    :return: 新建或加强的边数。
    :raises ValueError: 任一 ``ref_kind`` 非法。
    :raises sqlite3.Error: 读写失败。
    副作用：写入 ``memory_nodes`` 与 ``memory_edges`` 并提交事务。
    """
    unique = list(dict.fromkeys(refs))
    if len(unique) < 2:
        return 0
    ids = [node_id(db, kind, ref) for kind, ref in unique]
    touched = 0
    for index, source in enumerate(ids):
        for target in ids[index + 1:]:
            # 边无方向：按 (较小 ID, 较大 ID) 归一化存储，每对记忆仅保留一行。
            low, high = (source, target) if source < target else (target, source)
            row = db.execute(
                'SELECT strength FROM memory_edges WHERE source_id = ? AND target_id = ?',
                (low, high),
            ).fetchone()
            if row is None:
                db.execute(
                    'INSERT INTO memory_edges (source_id, target_id, strength, updated_at, active) '
                    'VALUES (?, ?, ?, ?, 1)',
                    (low, high, EDGE_BOOST, now),
                )
            else:
                db.execute(
                    'UPDATE memory_edges SET strength = ?, updated_at = ?, active = 1 '
                    'WHERE source_id = ? AND target_id = ?',
                    (reinforce(float(row[0]), EDGE_BOOST), now, low, high),
                )
            touched += 1
    db.commit()
    return touched


def decay_edges(db: sqlite3.Connection, now: int) -> int:
    """将强度低于冻结阈值的边置为非活跃。

    与 facts 的衰减策略一致：仅停用，不删除。两条记忆再次共同出现时，
    :func:`link_together` 会将边重新激活。

    :param db: 当前库连接。
    :param now: 当前毫秒时间戳。
    :return: 本次置为非活跃的边数。
    :raises sqlite3.Error: 读写失败。
    副作用：更新 ``memory_edges.active`` 并提交事务。
    """
    frozen = 0
    rows = db.execute(
        'SELECT id, strength, updated_at FROM memory_edges WHERE active = 1'
    ).fetchall()
    for edge_id, strength, updated_at in rows:
        if retention(float(strength), int(updated_at), EDGE_HALF_LIFE_HOURS, now) < FREEZE:
            db.execute('UPDATE memory_edges SET active = 0 WHERE id = ?', (edge_id,))
            frozen += 1
    if frozen:
        db.commit()
    return frozen


class ShortTermActivation:
    """进程内短期激活记录，用于提高近期使用过的节点在扩散中的权重。

    仅存于进程内存，不持久化：激活状态只反映当前会话的近期使用，
    进程重启后重建为空。
    """

    def __init__(self) -> None:
        """创建空的激活表。"""
        self._at: Dict[int, int] = {}

    def touch(self, node_ids: Sequence[int], now: int) -> None:
        """把这些节点的激活值置满。

        :param node_ids: 本轮真正被点亮的节点。
        :param now: 当前毫秒时间戳。
        副作用：更新进程内激活表，不写库。
        """
        for node in node_ids:
            self._at[node] = now

    def factor(self, node: int, now: int) -> float:
        """取一个节点当前的激活加成系数。

        :param node: 节点 ID。
        :param now: 当前毫秒时间戳。
        :return: ``1 + 激活值``，激活值按 :data:`ACTIVATION_TAU_MS` 指数衰减；
            从未被点亮时为 ``1.0``（不加成，也不惩罚）。
        """
        last = self._at.get(node)
        if last is None:
            return 1.0
        elapsed = max(0, now - last)
        return 1.0 + math.exp(-elapsed / ACTIVATION_TAU_MS)

    def clear(self) -> None:
        """清空激活表，供测试与进程内重置使用。"""
        self._at.clear()


def _neighbours(db: sqlite3.Connection, node: int) -> List[Tuple[int, float, int]]:
    """取一个节点的活跃邻居。

    :param db: 当前库连接。
    :param node: 节点 ID。
    :return: ``(邻居节点 ID, 边强度, 边更新时间)`` 列表。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    rows = db.execute(
        'SELECT source_id, target_id, strength, updated_at FROM memory_edges '
        'WHERE active = 1 AND (source_id = ? OR target_id = ?)',
        (node, node),
    ).fetchall()
    out: List[Tuple[int, float, int]] = []
    for source, target, strength, updated_at in rows:
        other = int(target) if int(source) == node else int(source)
        out.append((other, float(strength), int(updated_at)))
    return out


def _node_retention(db: sqlite3.Connection, node: int, now: int) -> Optional[float]:
    """取一个节点自身的留存度，用于给扩散结果打分。

    :param db: 当前库连接。
    :param node: 节点 ID。
    :param now: 当前毫秒时间戳。
    :return: 留存度；节点指向的记忆已不存在时返回 ``None``。
        知识层不衰减，恒为 ``1.0``；情节层没有强度列，同样按 ``1.0`` 处理。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    row = db.execute('SELECT ref_kind, ref_id FROM memory_nodes WHERE id = ?', (node,)).fetchone()
    if row is None:
        return None
    ref_kind, ref_id = str(row[0]), int(row[1])
    if ref_kind == 'fact':
        fact = db.execute(
            'SELECT strength, updated_at, half_life_hours FROM facts WHERE id = ? AND active = 1',
            (ref_id,),
        ).fetchone()
        if fact is None:
            return None
        return retention(float(fact[0]), int(fact[1]), float(fact[2]), now)
    table = 'episodes' if ref_kind == 'episode' else 'knowledge'
    exists = db.execute(f'SELECT 1 FROM {table} WHERE id = ?', (ref_id,)).fetchone()
    return 1.0 if exists is not None else None


def _seed_memory_nodes(
    db: sqlite3.Connection,
    seeds: Sequence[Tuple[str, int, float]],
) -> Dict[int, float]:
    """把三层检索命中解析成统一记忆图的个性化权重。"""
    seed_nodes: Dict[int, float] = {}
    for ref_kind, ref_id, relevance in seeds:
        row = db.execute(
            'SELECT id FROM memory_nodes WHERE ref_kind = ? AND ref_id = ?',
            (ref_kind, ref_id),
        ).fetchone()
        # 未登记的种子无关联边可走；读路径不能顺手创建指针。
        if row is not None:
            node = int(row[0])
            seed_nodes[node] = max(seed_nodes.get(node, 0.0), float(relevance))
    return seed_nodes


def _bounded_memory_graph(
    db: sqlite3.Connection,
    seed_nodes: Mapping[int, float],
    now: int,
    *,
    hops: int,
) -> Tuple[Dict[int, Dict[int, float]], Dict[int, int]]:
    """读取种子 ``hops`` 跳内的有效记忆子图与最短距离。"""
    adjacency: Dict[int, Dict[int, float]] = {node: {} for node in seed_nodes}
    distances = {node: 0 for node in seed_nodes}
    retention_cache: Dict[int, Optional[float]] = {}
    frontier = list(seed_nodes)
    for depth in range(hops):
        following: List[int] = []
        for node in frontier:
            for other, strength, _updated in _neighbours(db, node):
                keep = retention_cache.get(other)
                if other not in retention_cache:
                    keep = _node_retention(db, other, now)
                    retention_cache[other] = keep
                if keep is None or keep < REVIVE:
                    continue
                adjacency.setdefault(node, {})[other] = max(
                    adjacency.get(node, {}).get(other, 0.0), strength,
                )
                adjacency.setdefault(other, {})[node] = max(
                    adjacency.get(other, {}).get(node, 0.0), strength,
                )
                if other not in distances:
                    distances[other] = depth + 1
                    following.append(other)
        frontier = following
        if not frontier:
            break
    return adjacency, distances


def _bounded_graph(
    adjacency: Mapping[int, Mapping[int, float]],
    seeds: Mapping[int, float],
    hops: int,
) -> Tuple[Dict[int, Dict[int, float]], Dict[int, int]]:
    """从已加载邻接中截取限定跳数的诱导子图。"""
    distances = {node: 0 for node in seeds}
    frontier = list(seeds)
    for depth in range(hops):
        following: List[int] = []
        for node in frontier:
            for other in adjacency.get(node, {}):
                if other not in distances:
                    distances[other] = depth + 1
                    following.append(other)
        frontier = following
        if not frontier:
            break
    included = set(distances)
    bounded = {
        node: {
            other: weight
            for other, weight in adjacency.get(node, {}).items()
            if other in included
        }
        for node in distances
    }
    return bounded, distances


def _normalize_adjacency(
    adjacency: Mapping[int, Mapping[int, float]],
) -> Dict[int, Dict[int, float]]:
    """逐节点归一化出边；零出度节点保留为空邻接。"""
    normalized: Dict[int, Dict[int, float]] = {}
    for node, outgoing in adjacency.items():
        total = sum(float(weight) for weight in outgoing.values())
        normalized[node] = (
            {other: float(weight) / total for other, weight in outgoing.items()}
            if total > 0.0
            else {}
        )
    return normalized


def _memory_ppr(
    db: sqlite3.Connection,
    seed_nodes: Mapping[int, float],
    now: int,
    hops: int,
) -> Tuple[Dict[int, float], Dict[int, int]]:
    """在统一记忆图上运行 PPR，结果不含种子。"""
    if not seed_nodes or sum(seed_nodes.values()) <= 0.0:
        return {}, {}
    adjacency, distances = _bounded_memory_graph(db, seed_nodes, now, hops=hops)
    ranks = personalized_pagerank(
        adjacency,
        seed_nodes,
        alpha=tuned_ppr_alpha(),
        max_iterations=PPR_MAX_ITERATIONS,
        tolerance=PPR_TOLERANCE,
        timeout_seconds=PPR_TIMEOUT_SECONDS,
    )
    return (
        {node: score for node, score in ranks.items() if node not in seed_nodes},
        distances,
    )


def _ref_text(
    db: sqlite3.Connection,
    ref_kind: str,
    ref_id: int,
) -> Optional[str]:
    """读取一条三层记忆的正文，不接受动态表名。"""
    if ref_kind == 'fact':
        query = 'SELECT content FROM facts WHERE id = ?'
    elif ref_kind == 'episode':
        query = 'SELECT summary FROM episodes WHERE id = ?'
    elif ref_kind == 'knowledge':
        query = 'SELECT content FROM knowledge WHERE id = ?'
    else:
        return None
    row = db.execute(query, (ref_id,)).fetchone()
    return str(row[0]) if row is not None else None


def _load_knowledge_graph(
    db: sqlite3.Connection,
) -> Tuple[Dict[int, str], Dict[int, Dict[int, float]]]:
    """读取概念图，并把共现计数按每个节点的出度归一化。"""
    concepts = {
        int(row[0]): str(row[1])
        for row in db.execute('SELECT id, concept FROM knowledge_nodes ORDER BY id')
    }
    raw: Dict[int, Dict[int, float]] = {node: {} for node in concepts}
    for source, target, strength in db.execute(
        'SELECT source_id, target_id, strength FROM knowledge_edges ORDER BY id'
    ):
        left, right, weight = int(source), int(target), float(strength)
        if left not in concepts or right not in concepts or left == right:
            continue
        # 概念关系按既有 related_concepts 口径双向走；双向重复时只取最强一条。
        raw[left][right] = max(raw[left].get(right, 0.0), weight)
        raw[right][left] = max(raw[right].get(left, 0.0), weight)
    return concepts, _normalize_adjacency(raw)


def _concept_seed_weights(
    db: sqlite3.Connection,
    seeds: Sequence[Tuple[str, int, float]],
    concepts: Mapping[int, str],
) -> Dict[int, float]:
    """把种子正文中出现的概念转换成概念图个性化权重。"""
    weights: Dict[int, float] = {}
    for ref_kind, ref_id, relevance in seeds:
        text = _ref_text(db, ref_kind, ref_id)
        if text is None:
            continue
        folded = text.casefold()
        matched = [
            node
            for node, concept in concepts.items()
            if concept.strip() and concept.casefold() in folded
        ]
        if not matched:
            continue
        share = max(0.0, float(relevance)) / len(matched)
        for node in matched:
            weights[node] = weights.get(node, 0.0) + share
    return weights


def _memory_node_texts(db: sqlite3.Connection) -> Dict[int, str]:
    """读取所有已登记且仍有正文的记忆节点，供概念结果回映。"""
    rows = db.execute(
        '''SELECT mn.id,
                  CASE mn.ref_kind
                    WHEN 'fact' THEN f.content
                    WHEN 'episode' THEN e.summary
                    WHEN 'knowledge' THEN k.content
                  END AS text
           FROM memory_nodes mn
           LEFT JOIN facts f ON mn.ref_kind = 'fact' AND f.id = mn.ref_id
           LEFT JOIN episodes e ON mn.ref_kind = 'episode' AND e.id = mn.ref_id
           LEFT JOIN knowledge k ON mn.ref_kind = 'knowledge' AND k.id = mn.ref_id
           ORDER BY mn.id'''
    ).fetchall()
    return {int(node): str(text) for node, text in rows if text is not None}


def _knowledge_ppr(
    db: sqlite3.Connection,
    seeds: Sequence[Tuple[str, int, float]],
    seed_memory_nodes: Set[int],
    hops: int,
) -> Tuple[Dict[int, float], Dict[int, int]]:
    """在概念图独立运行 PPR，并把非种子概念回映到记忆节点。"""
    concepts, adjacency = _load_knowledge_graph(db)
    concept_seeds = _concept_seed_weights(db, seeds, concepts)
    if not concept_seeds or sum(concept_seeds.values()) <= 0.0:
        return {}, {}
    bounded, concept_distances = _bounded_graph(adjacency, concept_seeds, hops)
    # 截取子图后边界节点的出边集合发生变化，使用前必须再次按当前出度归一化。
    bounded = _normalize_adjacency(bounded)
    ranks = personalized_pagerank(
        bounded,
        concept_seeds,
        alpha=tuned_ppr_alpha(),
        max_iterations=PPR_MAX_ITERATIONS,
        tolerance=PPR_TOLERANCE,
        timeout_seconds=PPR_TIMEOUT_SECONDS,
    )

    node_texts = _memory_node_texts(db)
    scores: Dict[int, float] = {}
    distances: Dict[int, int] = {}
    for concept_node, score in ranks.items():
        if concept_node in concept_seeds:
            continue
        needle = concepts[concept_node].casefold()
        matched = [
            node
            for node, text in node_texts.items()
            if node not in seed_memory_nodes and needle in text.casefold()
        ]
        if not matched:
            continue
        # 一个概念命中多条正文时均分质量，避免通用概念凭文档数量重复放大。
        share = score / len(matched)
        for node in matched:
            scores[node] = scores.get(node, 0.0) + share
            hop = concept_distances[concept_node]
            distances[node] = min(distances.get(node, hop), hop)
    return scores, distances


def _hits_from_scores(
    db: sqlite3.Connection,
    scores: Mapping[int, float],
    distances: Mapping[int, int],
    now: int,
    activation: Optional[ShortTermActivation],
    limit: int,
) -> List[SpreadHit]:
    """融合两张图的概率质量，并套用节点生命周期与短期激活。"""
    hits: List[SpreadHit] = []
    for node, graph_score in scores.items():
        keep = _node_retention(db, node, now)
        if keep is None or keep < REVIVE:
            continue
        value = graph_score * keep
        if activation is not None:
            value *= activation.factor(node, now)
        row = db.execute(
            'SELECT ref_kind, ref_id FROM memory_nodes WHERE id = ?',
            (node,),
        ).fetchone()
        if row is None:
            continue
        hits.append(
            SpreadHit(
                node_id=node,
                ref_kind=str(row[0]),
                ref_id=int(row[1]),
                score=value,
                hops=distances[node],
            )
        )
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:limit]


def _legacy_spread(
    db: sqlite3.Connection,
    seeds: Sequence[Tuple[str, int, float]],
    now: int,
    *,
    hops: int = HOPS,
    limit: int = SPREAD_LIMIT,
    activation: Optional[ShortTermActivation] = None,
) -> List[SpreadHit]:
    """保留改造前扩散算法，仅供 PPR 超时时完整回退。"""
    if hops < 1 or limit < 1 or not seeds:
        return []
    seed_nodes = _seed_memory_nodes(db, seeds)
    best: Dict[int, Tuple[float, int]] = {}
    frontier = dict(seed_nodes)
    for hop in range(1, hops + 1):
        following: Dict[int, float] = {}
        for node, base in frontier.items():
            for other, strength, _updated in _neighbours(db, node):
                if other in seed_nodes:
                    continue
                keep = _node_retention(db, other, now)
                if keep is None or keep < REVIVE:
                    continue
                value = base * strength * HOP_DECAY * keep
                if activation is not None:
                    value *= activation.factor(other, now)
                if value > following.get(other, 0.0):
                    following[other] = value
        for node, value in following.items():
            if node not in best or value > best[node][0]:
                best[node] = (value, hop)
        frontier = following
        if not frontier:
            break

    hits: List[SpreadHit] = []
    for node, (value, hop) in best.items():
        row = db.execute(
            'SELECT ref_kind, ref_id FROM memory_nodes WHERE id = ?',
            (node,),
        ).fetchone()
        if row is None:
            continue
        hits.append(
            SpreadHit(
                node_id=node,
                ref_kind=str(row[0]),
                ref_id=int(row[1]),
                score=value,
                hops=hop,
            )
        )
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:limit]


def spread(
    db: sqlite3.Connection,
    seeds: Sequence[Tuple[str, int, float]],
    now: int,
    *,
    hops: Optional[int] = None,
    limit: int = SPREAD_LIMIT,
    activation: Optional[ShortTermActivation] = None,
) -> List[SpreadHit]:
    """从检索种子分别运行两张图的 PPR，再融合关联记忆。

    ``memory_edges`` 与 ``knowledge_edges`` 的节点空间和边权语义不同，绝不合图：
    前者直接在三层记忆指针上游走；后者先按每个概念的出度归一化共现计数，
    再把概念概率均分回正文命中的已登记记忆。两边各自得到概率分布后才相加，
    最后统一乘节点留存度与短期激活系数。

    PPR 超时时整次调用回退到改造前的固定跳数扩散，并发出
    ``memory_ppr_timeout`` 事件；其余错误保持暴露。结果不包含种子节点。

    :param db: 当前库连接。
    :param seeds: ``(ref_kind, ref_id, 相关度)`` 序列，来自现有的 recall / inspect。
    :param now: 当前毫秒时间戳。
    :param hops: 最多走几跳；``None`` 表示由检索调优的当前覆盖决定（未覆盖时取
        模块常量 ``HOPS``）；``0`` 表示整层关闭，直接返回空列表。
    :param limit: 结果条数上限。
    :param activation: 可选的短期激活表；省略时不加成。
    :return: 按融合分数降序的扩散命中，最多 ``limit`` 条。
    :raises sqlite3.Error: 查询或超时事件持久化失败。
    副作用：只读，不建边也不加强——建边只发生在「被采用」之后，由调用方显式调用
        :func:`link_together`。
    """
    hops = tuned_ppr_hops(HOPS) if hops is None else hops
    if hops < 1 or limit < 1 or not seeds:
        return []
    seed_nodes = _seed_memory_nodes(db, seeds)
    try:
        memory_scores, memory_distances = _memory_ppr(db, seed_nodes, now, hops)
    except PageRankTimeoutError:
        trace.emit('memory_ppr_timeout', graph='memory')
        return _legacy_spread(
            db, seeds, now, hops=hops, limit=limit, activation=activation,
        )
    try:
        knowledge_scores, knowledge_distances = _knowledge_ppr(
            db, seeds, set(seed_nodes), hops,
        )
    except PageRankTimeoutError:
        trace.emit('memory_ppr_timeout', graph='knowledge')
        return _legacy_spread(
            db, seeds, now, hops=hops, limit=limit, activation=activation,
        )

    fused = dict(memory_scores)
    distances = dict(memory_distances)
    for node, score in knowledge_scores.items():
        fused[node] = fused.get(node, 0.0) + score
        hop = knowledge_distances[node]
        distances[node] = min(distances.get(node, hop), hop)
    return _hits_from_scores(db, fused, distances, now, activation, limit)
