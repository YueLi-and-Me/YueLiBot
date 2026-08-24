"""联想召回层（W6）：让激活沿记忆之间的边扩散，而不只是查得到。

现有的 `recall` / `inspect` 都是**查询**——只能回答「你问的这条我有没有」。人的记忆
不是这样工作的：提到一个人的名字，跟着浮上来的是上次一起吃饭的馆子、那天下雨、他说的
那句玩笑，这些没有一个是查出来的，是被**牵**出来的。本模块补的就是这一步。

它**不新增存储层**，是长在 facts / episodes / knowledge 三层之上的一张边：

- ``memory_nodes`` 是纯指针表，把三层已有的记忆挂进同一个 id 空间。三层各有各的生命
  周期与衰减口径，合并成宽表是三份重复真相的老错误；而联想又需要统一 id 才能表达
  「这条 fact 和那段 episode 有关」。指针表是最小代价——删掉它不丢任何记忆。
- ``memory_edges`` 与 ``knowledge_edges`` **刻意不合并**：后者表达「概念 A 与概念 B
  相关」，本表表达「这两段记忆总是一起出现」，合并会让建边规则立刻分裂成两套。

明确不做的是另一种「神经元」：向量权重、训练、反向传播。那条路的产物是一堆浮点数，
「她为什么突然想到这个」既无法解释也无法回放，而观察面板是这个项目最贵的资产之一。
本层的每一步——命中了哪些种子、沿哪条边走、每跳衰减多少——都能在面板里逐条看出来。

对外暴露 :func:`node_id`（登记指针）、:func:`link_together`（赫布建边）、
:func:`spread`（扩散激活）与 :class:`ShortTermActivation`（进程内短期残留）。
被 ``agent/fact_extract``（同批写入建边）与 ``agent/cognition``（召回后扩散）使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import math
import sqlite3

from .decay import FREEZE, REVIVE, reinforce, retention

# 最多沿边走几跳。1 跳是「直接相关」，2 跳是「相关的相关」，再远就与当下无关了。
HOPS = 2
# 每跳的衰减系数：走得越远权重越低，避免远处的强边压过近处的弱边。
HOP_DECAY = 0.5
# 扩散结果的条数上限。它与种子分开计数——「顺带想起」不该挤占「她记得」的注入预算。
SPREAD_LIMIT = 4
# 建边与加强的步长，复用 reinforce 的既有默认值，不是新增常量。
EDGE_BOOST = 0.35
# 边的半衰期，与 facts 的 30 天默认同量级：一段时间不再一起出现的两条记忆会自然疏远。
EDGE_HALF_LIFE_HOURS = 720.0
# 短期激活的衰减时间常数（毫秒）。表达「刚才聊到过」，不落库。
ACTIVATION_TAU_MS = 10 * 60 * 1000

# 指针表允许的三种来源，写死而不是开放字符串：拼错的 ref_kind 会让边指向不存在的记忆，
# 而那种错不会报错，只会让扩散结果莫名其妙地空掉。
REF_KINDS = ('fact', 'episode', 'knowledge')


@dataclass(frozen=True)
class SpreadHit:
    """一条被牵出来的记忆。

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
    """把一组「一起被点亮」的记忆两两建边或加强。

    **唯一的建边时机就是一起被点亮**，只有两种：同一批被写入，或同一次召回里
    **真正进了提示词**的那些。被检索到不等于被用到——这个区分是边质量的全部来源
    （★W6-2），因此调用方必须只把实际采用的节点传进来。

    加强复用 :func:`~src.core.memory.decay.reinforce` 的饱和口径，不另写增长函数。

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
            # 边无方向，用 (小, 大) 归一化存一条，避免同一对记忆存成两行各自衰减。
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
    """把已经衰减到冻结线以下的边置为非活跃。

    **只置非活跃、绝不删除**，与 facts 的处置一致：这个项目一以贯之的口径是遗忘不等于
    抹除。跌破 :data:`~src.core.memory.decay.FREEZE` 的边停止参与扩散，但两条记忆再次
    一起出现时 :func:`link_together` 会把它重新激活。

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
    """进程内的短期激活残留：刚聊到过的记忆，这一轮更容易再被牵出来。

    **刻意不落库**：它表达的是「刚才聊到过」，进程重启后本来就该消失（★W6-6）。
    落库会让她重启之后仍然带着上一次对话的联想偏好，那不是记忆，是状态泄漏。

    解决的是连续对话里的联想连贯性——上一轮牵出来的东西这一轮更容易再现，
    而不是每轮从零重新联想。
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
    """从种子出发沿活跃边扩散，返回「顺带想起」的记忆。

    打分：``score(邻居) = score(来源) * 边强度 * HOP_DECAY * retention(邻居)``，
    再乘短期激活系数。三条约束都是刻意的：

    - **同一节点被多条路径命中时取最大值，不累加**（★W6-3）。累加会让连接多的节点
      永远排在前面——那是度数排序，不是相关度排序。
    - **邻居自身的留存度参与打分**（★W6-4）。一条正在被遗忘的记忆不该因为连着一条
      强边就被硬拽回来；跌破 :data:`~src.core.memory.decay.REVIVE` 的直接不出现。
    - **结果不含种子**。种子是「她记得的」，扩散结果是「她顺带想起的」，两者在提示词里
      语气不同，混在一起就没法分开标注了。

    :param db: 当前库连接。
    :param seeds: ``(ref_kind, ref_id, 相关度)`` 序列，来自现有的 recall / inspect。
    :param now: 当前毫秒时间戳。
    :param hops: 最多走几跳；``0`` 表示整层关闭，直接返回空列表（★W6-5）。
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
        # 种子没登记过说明它还没参与过任何一次共同点亮，自然也没有边可走。
        # 这里不顺手登记：读路径不该产生写副作用。
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
                # 取最大值而不是累加，见函数文档第一条约束。
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
