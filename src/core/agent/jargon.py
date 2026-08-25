"""黑话查表与命中：本轮上下文 → 纯子串匹配 → 打分排序 → ``jargon`` 表命中词条。

群里的人用一堆只有那个群才懂的说法，她按字面理解、按字面回话，就明显是个
外人。旧系统积累了约 1024 条已确认的「词 → 含义」，由 [W2 迁移] 落进
``jargon`` 表；本模块只负责**用起来**：给出本轮上下文里命中了哪些词，供提示词
渲染成一小段注入。**绝不整表倾倒**——真人也只是在别人用到某个词时才懂它，
1024 条全塞进提示词既浪费上下文，又会让她满嘴黑话。

设计要点：

- 匹配是**纯子串包含**（``term in text``），没有分词、没有 bigram、没有单字
  枚举。误报（「大」命中「大家」、「典」命中「字典」）**不在匹配层拦，在排序
  层沉底**：只查 confirmed 与单轮 5 条上限只是兜底，真正起作用的是打分。
- 打分依赖 :mod:`src.core.memory.high_frequency` 维护的会话高频词表：命中
  高频词表的词条拿到量级碾压的加分，没命中的只剩自身计数，被条数上限截光。
  高频词表收词要求 CJK ≥2 字，于是「大」「开」这类单字误报永远拿不到加分，
  而真单字黑话（咕、典、绷、孝）不被降级——它若在这个群真的高频，会作为
  词条自身的出现次数浮上来。
- ``hits`` 命中即加一、照记不误（那是调优数据），但**不参与排序**。旧实现按
  hits 降序截断，而 hits 含被截掉的命中，构成正反馈：越是误报的词 hits 涨得
  越快、下次排得越前——那是把误报往上顶的机制，不是沉底机制。
- 跨轮去重：同一个词在一段对话里只解释一次。已注入集合在进程内
  （:class:`InjectedTerms`），刻意不落库、重启即消失——「刚给解释过」是回合
  间的临时状态，落库会让她重启后仍带着上一次对话的解释记忆，那是状态泄漏。
- 只扫**他人消息**（工作记忆里 ``role='user'`` 的消息加本轮批次原文），排除
  bot 自己的发言：她不需要被科普自己说过的话。
- bot 自己的名字与别名（含对用户的称呼）永远不得注入：那是她自己的身份，
  不是黑话；见 :func:`lookup_jargon` 的 ``protected_names``。
- 查表按 ``(term, stream_id)``：先本会话专属、再全局（``stream_id IS NULL``），
  同一个词两边都有时本会话优先——同一个词在不同群含义可以不同，这正是
  ``stream_id`` 存在的理由。
- 只查 ``status = 'confirmed'``；pending 是只存不用的候选，永远不进提示词。
- 释义在注入前压缩到首句或 30 字：迁移数据的释义平均 271 字、六成带百科腔，
  原样贴出会让黑话块吃掉整份提示词的可观份额。截断口径只在本模块
  （:func:`compress_meaning`），``prompt.py`` 侧是纯渲染器不做二次截断，
  这是既有纪律。
- 词典整表只有千级行且常驻页缓存，按 scope 取回后内存匹配比拼装 IN 列表
  更直接，也不受绑定参数个数上限牵制。

本模块不做自动学习、不做候选表、不做衰减：黑话是词典不是记忆，一个词的
含义不会因为三个月没人用就失效。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Collection, Dict, List, Optional, Sequence, Tuple

# 单轮注入黑话条数上限，本块唯一的常量：一轮命中十几个词说明要么语料太杂、
# 要么那批词条质量有问题，注入更多只会稀释真正相关的那几条。它只在这里
# 执行——prompt 侧是纯渲染器，不做二次截断。打分权重与去重 TTL 等调节
# 常量写在各自函数旁，不堆到这一块。
MAX_INJECTED_JARGON = 5

# 释义压缩上限（字符）。取首句（首个句末标点或换行之前），首句超长再硬截到
# 该上限；上限含结尾省略号，保证压缩结果整体不超过 30 字。
_MEANING_LIMIT = 30
_SENTENCE_ENDINGS = '。！？'

# 命中词条的展示形态：(词, 含义)，与 jargon 表的 term/meaning 列一一对应。
JargonEntry = Tuple[str, str]


def compress_meaning(meaning: str) -> str:
    """把黑话释义压缩到首句或 30 字以内。

    迁移进来的释义平均 271 字、六成带百科腔（「『x』一词源自日语……」这类），
    原样注入会把黑话块撑到占整份提示词近 8%。真人解释一个梗也只说一句，
    首句足够她听懂语境。

    :param meaning: jargon 表里原样的释义文本。
    :return: 首个 ``。！？\\n`` 之前的短句；首句本身超长时硬截并带省略号；
        空白输入返回空字符串。
    副作用：无——只压缩展示文本，不回写 jargon 表。
    """
    text = (meaning or '').strip()
    if not text:
        return ''
    cut = len(text)
    for index, char in enumerate(text):
        if char in _SENTENCE_ENDINGS:
            cut = index + 1
            break
        if char == '\n':
            cut = index
            break
    first_sentence = text[:cut].strip()
    if len(first_sentence) <= _MEANING_LIMIT:
        return first_sentence
    return first_sentence[:_MEANING_LIMIT - 1] + '…'


# 已注入词条的残留时长（毫秒）。「一段对话」没有硬边界，用 30 分钟近似：
# 间隔更久的两条消息之间，话题大概率已经换过，再解释一次是正常行为。
INJECTED_TTL_MS = 30 * 60 * 1000


class InjectedTerms:
    """进程内的会话级已注入词条集合：同一个词在一段对话里只解释一次。

    **刻意不落库**：它表达的是「刚才解释过」，进程重启后本来就该消失（与
    ``memory/association.py`` 的 ``ShortTermActivation`` 同一条理由）。落库
    会让她重启之后仍然记得上一次对话解释过什么，那不是记忆，是状态泄漏。
    """

    def __init__(self) -> None:
        """创建空的已注入表。"""
        self._at: Dict[Tuple[int, str], int] = {}

    def recent(self, stream_id: int, term: str, now: int) -> bool:
        """判断一个词条在该会话的残留期内是否已注入过。

        :param stream_id: 会话 ID；不同会话互不影响。
        :param term: 已归一化（去空白、小写）的词条。
        :param now: 当前毫秒时间戳。
        :return: 残留期内注入过返回 ``True``。
        副作用：顺手清理全表过期项，不写库。
        """
        cutoff = now - INJECTED_TTL_MS
        for key in [key for key, at in self._at.items() if at <= cutoff]:
            del self._at[key]
        return self._at.get((stream_id, term), 0) > cutoff

    def record(self, stream_id: int, terms: Sequence[str], now: int) -> None:
        """把本回合真正注入的词条登记进残留表。

        :param stream_id: 会话 ID。
        :param terms: 已归一化的词条序列。
        :param now: 当前毫秒时间戳。
        副作用：更新进程内残留表，不写库。
        """
        for term in terms:
            self._at[(stream_id, term)] = now


@dataclass
class _Match:
    """一条通过子串匹配命中的词条及其在扫描语料里的统计。"""

    row: sqlite3.Row
    # 词条在扫描语料里的总出现次数（打分的主体）。
    count: int
    # 首次命中的消息下标（时间顺序，越小越早）。
    first_index: int


# 命中会话高频词的基础加分。它不是魔法数：含义是「命中高频词」这件事必须
# 压倒词条自身的任何出现计数——工作记忆窗口内的计数是十位数量级，高频词表
# 里另有整周累计的出现次数参与加分，1000 保证两者不曾在同一量级上掰手腕。
_HIGH_FREQUENCY_BASE_BONUS = 1000.0
# 首次命中位置的惩罚系数：越早被人提起越相关；但 0.01/条的量级只是稳定排序
# 的微调，几十条消息全加起来也盖不过一次出现。
_POSITION_PENALTY = 0.01


def _rank_matches(
    matches: Dict[str, _Match],
    high_frequency: Dict[str, Tuple[int, int]],
) -> List[Tuple[str, _Match]]:
    """按打分给命中词条排序。

    计分式：

    ``score = 词条自身出现次数 + 高频词加分 - 首次命中的消息位置 * 0.01``

    其中 ``高频词加分 = 1000 + 该词在本会话的出现次数 * 2 + max(0, 100 - 排名)``，
    仅当词条命中该会话的高频词表时计入。于是：命中高频词的多字黑话以千级
    分碾压；没命中的单字误报只剩自身个位数计数，被条数上限截光——沉底不发
    生在匹配层，发生在这里。

    :param matches: 归一化词条到命中统计的映射。
    :param high_frequency: 该会话高频词表，词条到 ``(出现次数, 名次)`` 的映射。
    :return: ``(归一化词条, 命中统计)`` 列表，按 score 降序 → 首次命中位置
        升序 → 词长降序 → 词面 排序；同分时长词优先。
    副作用：无。
    """
    scored: List[Tuple[float, int, int, str, _Match]] = []
    for term, match in matches.items():
        bonus = 0.0
        stats = high_frequency.get(term)
        if stats is not None:
            occurrence_count, rank = stats
            bonus = (_HIGH_FREQUENCY_BASE_BONUS
                     + occurrence_count * 2
                     + max(0, 100 - rank))
        score = match.count + bonus - match.first_index * _POSITION_PENALTY
        scored.append((score, match.first_index, -len(term), term, match))
    # score 降序 → 首次命中位置升序 → 词长降序 → 词面；后三项是同分裁决。
    scored.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    return [(term, match) for _, _, _, term, match in scored]


def lookup_jargon(
    db: sqlite3.Connection,
    stream_id: int,
    scan_texts: Sequence[str],
    *,
    protected_names: Collection[str] = (),
    injected: Optional[InjectedTerms] = None,
    now: Optional[int] = None,
) -> List[JargonEntry]:
    """查表并返回本轮上下文命中的已确认黑话。

    流程：他人消息语料 → 查两个 scope 的 confirmed 词条 → 纯子串匹配统计
    → 排除残留期内已注入过的 → ``hits`` 全部加一（照记，不参与排序）→
    按第 2 节公式打分排序（同分长词优先）→ 去重登记 → 截断到
    :data:`MAX_INJECTED_JARGON` 条 → 压缩释义。

    :param db: 进程级 SQLite 连接（与 MemoryStore 同一来源）。
    :param stream_id: 当前会话 ID；专属词条只在该会话生效。
    :param scan_texts: 他人消息文本，时间顺序（历史在前、本轮批次原文在末尾）；
        只命中这里出现过的词。
    :param protected_names: 永不作为黑话注入的名字（bot 名、别名与对用户的
        称呼）；按归一化后的词面比对，不受词条 status 与作用域影响。
    :param injected: 会话级已注入集合；省略时不做跨轮去重。
    :param now: 当前毫秒时间戳；省略时取系统时钟（仅供去重残留判定）。
    :return: 至多 5 个 ``(词, 含义)`` 二元组，含义已压缩；无命中时为空列表。
    :raises sqlite3.Error: 查询或写入 hits 失败时抛出。
    副作用：
        对命中的词条执行 ``hits = hits + 1`` 并提交——含被去重排除、被上限
        截掉、没进提示词的那些（命中发生在查表，截断发生在注入）；向
        ``injected`` 登记真正注入的词条。
    """
    texts = [
        text.strip().lower()
        for text in ([scan_texts] if isinstance(scan_texts, str) else scan_texts)
        if text and text.strip()
    ]
    if not texts:
        return []
    guarded = {name.strip().lower() for name in protected_names if name and name.strip()}
    rows = db.execute(
        '''SELECT id, term, meaning, hits, stream_id FROM jargon
           WHERE status = 'confirmed' AND (stream_id = ? OR stream_id IS NULL)''',
        (stream_id,),
    ).fetchall()
    matched: Dict[str, _Match] = {}
    for row in rows:
        key = row['term'].strip().lower()
        # 她自己的名字与别名不是黑话：任何 status、任何路径下都不得注入。
        if not key or key in guarded:
            continue
        count = 0
        first_index = len(texts)
        for index, text in enumerate(texts):
            occurrences = text.count(key)
            if occurrences:
                count += occurrences
                first_index = min(first_index, index)
        if not count:
            continue
        previous = matched.get(key)
        # 本会话优先：已有专属词条时全局让位；两个都是全局/专属时保留先见的。
        if previous is None or (previous.row['stream_id'] is None
                                and row['stream_id'] is not None):
            matched[key] = _Match(row=row, count=count, first_index=first_index)
    if not matched:
        return []
    db.executemany(
        'UPDATE jargon SET hits = hits + 1 WHERE id = ?',
        [(match.row['id'],) for match in matched.values()],
    )
    db.commit()
    stamp = now if now is not None else int(time.time() * 1000)
    fresh = matched
    if injected is not None:
        # 同一个词在一段对话里只解释一次；被排除的词条 hits 已照记。
        fresh = {
            term: match for term, match in matched.items()
            if not injected.recent(stream_id, term, stamp)
        }
        if not fresh:
            return []
    high_frequency = {
        str(row['term']): (int(row['occurrence_count']), int(row['rank']))
        for row in db.execute(
            '''SELECT term, occurrence_count, rank FROM high_frequency_terms
               WHERE stream_id = ?''',
            (stream_id,),
        )
    }
    ranked = _rank_matches(fresh, high_frequency)
    selected = ranked[:MAX_INJECTED_JARGON]
    if injected is not None:
        injected.record(stream_id, [term for term, _ in selected], stamp)
    return [
        (match.row['term'].strip(), compress_meaning(match.row['meaning']))
        for _, match in selected
    ]
