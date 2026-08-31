"""表达方式候选池：从 ``expressions`` 表按会话加权抽样，并渲染注入文本。

表达方式来源为 ``expressions`` 表中从真实对话学到的「情境 → 句式」条目。
本模块负责：按 ``stream_id`` 取出当前会话的候选池（加权抽样），以及将选中的
样本渲染为提示词文本块。候选数量截断只发生在取池阶段，提示词侧为纯渲染器。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple
import random
import sqlite3


@dataclass(frozen=True)
class ExpressionSample:
    """一条表达方式：「在什么情境下用什么句式」的二元组加表行主键。

    :ivar id: ``expressions`` 表主键，选中回写 ``use_count`` 时按它定位。
    :ivar situation: 适用情境描述；选择提示词只向模型展示这一字段。
    :ivar style: 该情境下的说法示例；选中之后才作为载荷拼进注入文本。
    """

    id: int
    situation: str
    style: str


# 候选总数低于此数时本轮不选：候选过少时无贴合样本可选。
MIN_POOL_CANDIDATES = 10
# 高频子集（use_count > 1）达到此数才先从中抽一轮；不足时整轮抽样退化为只从全量抽。
_MIN_HIGH_FREQ = 10
# 两轮各自的抽样条数：高频一轮、全量一轮，去重合并后候选池至多 10 条。
_DRAW_PER_ROUND = 5
# count 线性映射的权重区间：最高频最多 5 倍权重，长尾始终保有非零概率。
_WEIGHT_LO = 1.0
_WEIGHT_HI = 5.0


def _linear_weights(counts: Sequence[int]) -> List[float]:
    """把一组 use_count 线性映射到 [_WEIGHT_LO, _WEIGHT_HI] 的权重。

    映射在本组候选的值域内进行：最低频取 1，最高频取 5；全组同频时权重全为 1，
    退化为均匀抽样。
    """

    lo, hi = min(counts), max(counts)
    if hi == lo:
        return [_WEIGHT_LO] * len(counts)
    span = hi - lo
    return [
        _WEIGHT_LO + (_WEIGHT_HI - _WEIGHT_LO) * (count - lo) / span
        for count in counts
    ]


def _weighted_sample(
    rows: Sequence[sqlite3.Row],
    k: int,
    rng: random.Random,
) -> List[sqlite3.Row]:
    """按 use_count 线性权重无放回抽取至多 k 行。"""

    pool = list(rows)
    picked: List[sqlite3.Row] = []
    while pool and len(picked) < k:
        weights = _linear_weights([int(row['use_count']) for row in pool])
        # random.choices 单抽一次后把命中行移出候选，等价于无放回的加权抽样。
        chosen = rng.choices(pool, weights=weights, k=1)[0]
        pool.remove(chosen)
        picked.append(chosen)
    return picked


def fetch_expression_pool(
    db: sqlite3.Connection,
    stream_id: int,
    rng: Optional[random.Random] = None,
) -> Tuple[List[ExpressionSample], int]:
    """取出当前会话的表达方式候选池与该会话的候选总数。

    抽样口径：

    1. 候选总数小于 :data:`MIN_POOL_CANDIDATES` 时不选，返回空池；
    2. 高频子集（``use_count > 1``）达到 10 条时，先从中加权抽 5 条；
    3. 再从全量候选加权抽 5 条，与高频结果按行去重合并，候选池至多 10 条。

    加权抽样的权重是 use_count 在候选组内线性映射到 [1, 5]：最高频最多 5 倍
    权重，长尾始终保有非零概率。

    ``checked`` 是放行闸门：仅 ``1``（人工确认）进池，``0``（未复核）与
    ``-1``（人工驳回）不进。闸门隔离 Bot 说过的记录与注入的表达，学习侧的
    自强化闭环问题与依据见 ``agent/expression_learn.py`` 模块文档。

    复核进度落后时，该会话候选可能不足 :data:`MIN_POOL_CANDIDATES`，表达注入
    停摆。该停摆为预期行为：不注入未经人工确认的内容。停摆由调用方的
    ``expression_select`` 观测事件上报。

    :param db: 进程级 SQLite 连接（与 MemoryStore 同一来源）。
    :param stream_id: 当前会话 ID；候选池严格按会话隔离，不跨会话借。
    :param rng: 可选的随机数生成器；省略时使用模块级随机源。
    :return: ``(候选池, 该会话候选总数)`` 二元组；池为空时总数仍如实返回。
    :raises sqlite3.Error: 查询 expressions 失败时抛出。
    副作用：只读数据库，不修改任何表。
    :performance: 单会话候选为千级行且常驻页缓存，整取后内存抽样比拼装
        ORDER BY RANDOM() 更直接，也便于权重口径单测。
    """

    rows = db.execute(
        'SELECT id, situation, style, use_count FROM expressions'
        ' WHERE stream_id = ? AND checked = 1',
        (stream_id,),
    ).fetchall()
    total = len(rows)
    if total < MIN_POOL_CANDIDATES:
        return [], total

    picker = rng or random
    picked: List[sqlite3.Row] = []
    high_freq = [row for row in rows if int(row['use_count']) > 1]
    if len(high_freq) >= _MIN_HIGH_FREQ:
        picked.extend(_weighted_sample(high_freq, _DRAW_PER_ROUND, picker))
    picked.extend(_weighted_sample(rows, _DRAW_PER_ROUND, picker))

    seen: set[int] = set()
    pool: List[ExpressionSample] = []
    for row in picked:
        row_id = int(row['id'])
        if row_id in seen:
            continue
        seen.add(row_id)
        pool.append(ExpressionSample(
            id=row_id,
            situation=str(row['situation']),
            style=str(row['style']),
        ))
    return pool, total


def render_expression_habits(samples: Sequence[ExpressionSample]) -> str:
    """把选择器挑中的表达样本渲染为提示词中的注入文本块。

    :param samples: 已选中的表达样本序列；空序列表示本轮不注入该提示词块。

    :return: 说明行加「当“{situation}”时，可以用“{style}”来表达。」的项目列表；
        输入为空时返回空字符串。

    :raises TypeError: 样本字段不是可格式化文本时由字符串格式化操作触发。
    """

    if not samples:
        return ''
    return '\n'.join([
        '【表达习惯参考，请视情况自然的使用】',
        *[f'- 当“{sample.situation}”时，可以用“{sample.style}”来表达。' for sample in samples],
    ])
