"""事实、情节与知识三层记忆之间的关联边存储与扩散召回。

``memory_nodes`` 将三层已有记录映射到统一节点 ID；``memory_edges`` 保存节点间的
无向关联边。扩散与建边的中间结果均以结构化数据落账，可在观察面板中回放。

对外暴露 :func:`node_id`、:func:`link_together`、:func:`spread` 与
:class:`ShortTermActivation`。被 ``agent/fact_extract``（写入后建边）与
``agent/cognition``（召回后扩散）使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import math
import sqlite3

from .decay import FREEZE, REVIVE, reinforce, retention

# 沿边扩散的最大跳数。
HOPS = 2
# 每跳的权重衰减系数。
HOP_DECAY = 0.5
# 单次扩散返回的结果条数上限。
SPREAD_LIMIT = 4
# 建边与加强时的强度增量。
EDGE_BOOST = 0.35
# 边强度的半衰期，单位小时。
EDGE_HALF_LIFE_HOURS = 720.0
# 短期激活的衰减时间常数，单位毫秒。
ACTIVATION_TAU_MS = 10 * 60 * 1000

# ``memory_nodes.ref_kind`` 的合法取值。
REF_KINDS = ('fact', 'episode', 'knowledge')


@dataclass(frozen=True)
class SpreadHit:
    """一次扩散命中的记忆。

    :ivar node_id: ``memory_nodes`` 主键。
    :ivar ref_kind: 来源层，``fact`` / ``episode`` / ``knowledge``。
    :ivar ref_id: 该层内的主键。
    :ivar score: 扩散打分，已含边强度、跳数衰减与邻居自身的留存度。
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


def spread(
    db: sqlite3.Connection,
    seeds: Sequence[Tuple[str, int, float]],
    now: int,
    *,
    hops: int = HOPS,
    limit: int = SPREAD_LIMIT,
    activation: Optional[ShortTermActivation] = None,
) -> List[SpreadHit]:
    """从种子节点沿活跃边扩散，返回关联记忆。

    打分公式：``score(邻居) = score(来源) * 边强度 * HOP_DECAY * retention(邻居)``，
    再乘短期激活系数。约束：

    - 同一节点经多条路径命中时取最大值，不累加。
    - 邻居自身的留存度参与打分；留存度低于 :data:`~src.core.memory.decay.REVIVE`
      的节点不返回。
    - 结果不包含种子节点。

    :param db: 当前库连接。
    :param seeds: ``(ref_kind, ref_id, 相关度)`` 序列，来自现有的 recall / inspect。
    :param now: 当前毫秒时间戳。
    :param hops: 最多走几跳；``0`` 表示整层关闭，直接返回空列表。
    :param limit: 结果条数上限。
    :param activation: 可选的短期激活表；省略时不加成。
    :return: 按 score 降序的扩散命中，最多 ``limit`` 条。
    :raises sqlite3.Error: 查询失败。
    副作用：只读，不建边也不加强——建边只发生在「被采用」之后，由调用方显式调用
        :func:`link_together`。
    """
    if hops < 1 or limit < 1 or not seeds:
        return []
    seed_nodes: Dict[int, float] = {}
    for ref_kind, ref_id, relevance in seeds:
        row = db.execute(
            'SELECT id FROM memory_nodes WHERE ref_kind = ? AND ref_id = ?', (ref_kind, ref_id)
        ).fetchone()
        # 未登记的种子无关联边可走，跳过；读路径不产生写入副作用。
        if row is not None:
            seed_nodes[int(row[0])] = max(seed_nodes.get(int(row[0]), 0.0), float(relevance))

    best: Dict[int, Tuple[float, int]] = {}
    frontier = dict(seed_nodes)
    for hop in range(1, hops + 1):
        nxt: Dict[int, float] = {}
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
                # 多路径命中时取最大值，不累加，见函数文档。
                if value > nxt.get(other, 0.0):
                    nxt[other] = value
        for node, value in nxt.items():
            if node not in best or value > best[node][0]:
                best[node] = (value, hop)
        frontier = nxt
        if not frontier:
            break

    hits: List[SpreadHit] = []
    for node, (value, hop) in best.items():
        row = db.execute('SELECT ref_kind, ref_id FROM memory_nodes WHERE id = ?', (node,)).fetchone()
        if row is None:
            continue
        hits.append(SpreadHit(
            node_id=node, ref_kind=str(row[0]), ref_id=int(row[1]), score=value, hops=hop,
        ))
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:limit]
