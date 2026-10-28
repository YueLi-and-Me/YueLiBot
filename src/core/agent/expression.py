"""表达方式候选池：从 ``expressions`` 表按会话加权抽样，并渲染注入文本。

表达方式来源为 ``expressions`` 表中从真实对话学到的「情境 → 句式」条目。
本模块负责：按 ``stream_id`` 取出当前会话的候选池（加权抽样），以及将选中的
样本渲染为提示词文本块。候选数量截断只发生在取池阶段，提示词侧为纯渲染器。

抽样权重由「被学到次数」（``learn_count``）决定并乘以冷却系数；``use_count``
只记录本机被选中次数，供审核页展示，不参与抽样。旧口径把 ``use_count`` 同时当
权重，导致「越被选中越容易被再选中」的正反馈，头部条目的选中占比因此高到失真。
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
# 高频子集（learn_count > 1，即被学到不止一次）达到此数才先从中抽一轮；
# 不足时整轮抽样退化为只从全量抽。
_MIN_HIGH_FREQ = 10
# 两轮各自的抽样条数：高频一轮、全量一轮，去重合并后候选池至多 10 条。
_DRAW_PER_ROUND = 5
# learn_count 线性映射的权重区间：学得最多的条目最多 5 倍权重，长尾始终保有非零概率。
_WEIGHT_LO = 1.0
_WEIGHT_HI = 5.0
# 冷却时间窗 τ，单位毫秒，取值 6 小时（6 * 60 * 60 * 1000）。
# 含义：距上次被选中不足 τ 时权重按 min(1, Δt / τ) 衰减，刚用过的条目本轮不进抽样。
# 取 6 小时的理由：生产数据里群 5 每天约 13 次选择、候选仅几十条，6 小时内同一句
# 基本不会再出现；再长会把可用候选压到下限以下，再短则挡不住头部集中（方案第三节）。
EXPRESSION_COOLDOWN_MS = 6 * 60 * 60 * 1000
# situation 的合法时间尾缀，按从长到短排列。学习侧要求情境写成「…时」这种关系句
# （任务书 T4 第 3 条），而渲染模板固定补一个「时」；渲染前去掉一个尾缀，避免
# 拼出「当时时」。无尾缀的情境原样套模板，输出与契约逐字一致。
_SITUATION_TIME_SUFFIXES = ('的时候', '时')
# 旧条目（迁移存量）的 situation 常以句末标点收尾；渲染前与尾随空白一并去掉，
# 否则模板会拼出「…的反应。时」。单位字符，取中英文常见句末标点。
_SITUATION_TRAILING_PUNCTUATION = frozenset('。！？.!?…')


def _linear_weights(counts: Sequence[int]) -> List[float]:
    """把一组 learn_count 线性映射到 [_WEIGHT_LO, _WEIGHT_HI] 的权重。

    映射在参与本轮映射的候选值域内进行：最低频取 1，最高频取 5；整组同频时
    权重全为 1，退化为均匀抽样。

    :param counts: 本组候选的 ``learn_count`` 列表。
    :return: 与 ``counts`` 等长的权重列表，值域 [_WEIGHT_LO, _WEIGHT_HI]。
    副作用：无。
    """

    lo, hi = min(counts), max(counts)
    if hi == lo:
        return [_WEIGHT_LO] * len(counts)
    span = hi - lo
    return [
        _WEIGHT_LO + (_WEIGHT_HI - _WEIGHT_LO) * (count - lo) / span
        for count in counts
    ]


def _cooldown_factor(last_used_at: Optional[int], now: int) -> float:
    """计算一条表达的冷却系数。

    一条表达只属于一个会话（``stream_id``），候选池又严格按会话取，因此
    ``last_used_at`` 就是「该会话内上次使用时间」，不需要单独建冷却表。

    :param last_used_at: 上次被选入提示词的毫秒时间戳；从未被选中时为 ``None``。
    :param now: 当前毫秒时间戳，由调用方传入统一时钟。
    :return: [0.0, 1.0] 区间的系数：从未使用为 1.0；距上次使用达到
        :data:`EXPRESSION_COOLDOWN_MS` 后恢复为 1.0；刚用过为 0.0。
    副作用：无。
    """

    if last_used_at is None:
        return 1.0
    elapsed = now - int(last_used_at)
    return max(0.0, min(1.0, elapsed / EXPRESSION_COOLDOWN_MS))


def _weighted_sample(
    rows: Sequence[sqlite3.Row],
    k: int,
    rng: random.Random,
    *,
    now: int,
) -> List[sqlite3.Row]:
    """按 learn_count 线性权重乘冷却系数无放回抽取至多 k 行。

    :param rows: 候选行，须含 ``learn_count`` 与 ``last_used_at`` 列；调用方
        保证冷却系数为 0 的行不在其中。
    :param k: 最多抽取的行数。
    :param rng: 随机数来源，由调用方注入以保证可复现。
    :param now: 当前毫秒时间戳，用于计算冷却系数。
    :return: 抽中的行，按抽中顺序排列。
    副作用：无。
    """

    pool = list(rows)
    picked: List[sqlite3.Row] = []
    while pool and len(picked) < k:
        base = _linear_weights([int(row['learn_count']) for row in pool])
        weights = [
            weight * _cooldown_factor(row['last_used_at'], now)
            for weight, row in zip(base, pool)
        ]
        # random.choices 单抽一次后把命中行移出候选，等价于无放回的加权抽样。
        chosen = rng.choices(pool, weights=weights, k=1)[0]
        pool.remove(chosen)
        picked.append(chosen)
    return picked


def fetch_expression_pool(
    db: sqlite3.Connection,
    stream_id: int,
    *,
    now: int,
    rng: Optional[random.Random] = None,
) -> Tuple[List[ExpressionSample], int]:
    """取出当前会话的表达方式候选池与该会话的候选总数。

    抽样口径：

    1. 候选总数小于 :data:`MIN_POOL_CANDIDATES` 时不选，返回空池；
    2. 先剔除冷却系数为 0 的条目（刚用过），剩余不足
       :data:`MIN_POOL_CANDIDATES` 时本轮不选，返回空池；
    3. 高频子集（``learn_count > 1``）在剩余候选里达到 10 条时，先从中加权抽 5 条；
    4. 再从剩余候选全量加权抽 5 条，与高频结果按行去重合并，候选池至多 10 条。

    加权抽样的权重是 ``learn_count`` 在参与本轮映射的行内线性映射到 [1, 5]，
    再乘以该行的冷却系数 :func:`_cooldown_factor`：学得最多的条目最多 5 倍权重，
    长尾始终保有非零概率，刚用过的条目按比例降温。``use_count`` 只表示本机被
    选中次数，供审核页展示，不参与抽样。

    ``checked`` 是放行闸门：仅 ``1``（人工确认）进池，``0``（未复核）与
    ``-1``（人工驳回）不进。闸门隔离 Bot 说过的记录与注入的表达，学习侧的
    自强化闭环问题与依据见 ``agent/expression_learn.py`` 模块文档。

    复核进度落后时，该会话候选可能不足 :data:`MIN_POOL_CANDIDATES`，表达注入
    停摆。该停摆为预期行为：不注入未经人工确认的内容。停摆由调用方的
    ``expression_select`` 观测事件上报。全部条目都在冷却时同样按候选不足处理：
    本轮不选但不报错，避免刚说过话的会话把整轮回复拖失败。

    :param db: 进程级 SQLite 连接（与 MemoryStore 同一来源）。
    :param stream_id: 当前会话 ID；候选池严格按会话隔离，不跨会话借。
    :param now: 当前毫秒时间戳；由调用方传入，保证同一回合内取池与回写
        ``last_used_at`` 使用同一个时钟值。
    :param rng: 可选的随机数生成器；省略时使用模块级随机源。
    :return: ``(候选池, 该会话候选总数)`` 二元组；池为空时总数仍如实返回，
        且总数只统计 ``checked = 1`` 的行，不受冷却过滤影响。
    :raises sqlite3.Error: 查询 expressions 失败时抛出。
    副作用：只读数据库，不修改任何表。
    :performance: 单会话候选为千级行且常驻页缓存，整取后内存抽样比拼装
        ORDER BY RANDOM() 更直接，也便于权重口径单测。
    """

    rows = db.execute(
        'SELECT id, situation, style, learn_count, last_used_at FROM expressions'
        ' WHERE stream_id = ? AND checked = 1',
        (stream_id,),
    ).fetchall()
    total = len(rows)
    if total < MIN_POOL_CANDIDATES:
        return [], total

    eligible = [
        row for row in rows
        if _cooldown_factor(row['last_used_at'], now) > 0.0
    ]
    if len(eligible) < MIN_POOL_CANDIDATES:
        return [], total

    picker = rng or random
    picked: List[sqlite3.Row] = []
    high_freq = [row for row in eligible if int(row['learn_count']) > 1]
    if len(high_freq) >= _MIN_HIGH_FREQ:
        picked.extend(_weighted_sample(high_freq, _DRAW_PER_ROUND, picker, now=now))
    picked.extend(_weighted_sample(eligible, _DRAW_PER_ROUND, picker, now=now))

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


def _trim_situation_time_suffix(situation: str) -> str:
    """去掉 situation 结尾的句末标点、空白与时间尾缀，再套渲染模板。

    旧条目（迁移存量）的 situation 多写成「…时的反应。」这类以句号收尾的描述，
    新学习条目则按提示词要求以「…时」收尾；模板固定补一个「时」。因此按任务书
    设计决定 6 的顺序处理：先去结尾的「。！？.!?…」与空白，再去一个「时」或
    「的时候」；末尾若又露出标点再清一次。两种尾缀都不命中时原样返回。

    :param situation: 学习侧或存量条目给出的情境描述。
    :return: 适合直接拼进「当{situation}时」模板的情境文本。
    副作用：无。
    """

    text = situation.strip()
    while text and (
        text[-1] in _SITUATION_TRAILING_PUNCTUATION or text[-1].isspace()
    ):
        text = text[:-1].rstrip()
    for suffix in _SITUATION_TIME_SUFFIXES:
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    while text and (
        text[-1] in _SITUATION_TRAILING_PUNCTUATION or text[-1].isspace()
    ):
        text = text[:-1].rstrip()
    return text


def render_expression_habits(samples: Sequence[ExpressionSample]) -> str:
    """把选择器挑中的表达样本渲染为提示词中的注入文本块。

    :param samples: 已选中的表达样本序列；空序列表示本轮不注入该提示词块。

    :return: 说明行加「- 当{situation}时，可以套用：{style}」的项目列表；选中
        条目里至少一条 style 带「〈角色〉」占位时，末尾追加一行换算说明。输入
        为空时返回空字符串。旧条目没有〈角色〉，原样渲染。

    学习侧要求 situation 以「…时 / …的时候」收尾，而模板固定补一个「时」，因此
    渲染前去掉一个结尾的「时 / 的时候」，避免出现「当时时」；无尾缀时输出与模板
    逐字一致。

    :raises TypeError: 样本字段不是可格式化文本时由字符串格式化操作触发。
    """

    if not samples:
        return ''
    lines = [
        '【表达习惯参考，请视情况自然的使用】',
        *[
            f'- 当{_trim_situation_time_suffix(sample.situation)}时，可以套用：{sample.style}'
            for sample in samples
        ],
    ]
    # 〈角色〉是学习侧把「这次成立的人和物」抽象成占位符的写法；不提示换算，
    # 模型会把〈提出设想的人〉原样说进回复里。
    if any('〈' in sample.style for sample in samples):
        lines.append('〈〉里换成这次对话里对应的人和事；只借说法的结构，不要照搬原话。')
    return '\n'.join(lines)
