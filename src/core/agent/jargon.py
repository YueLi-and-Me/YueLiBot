"""黑话查表与命中：扫描本轮上下文，返回 ``jargon`` 表命中的已确认词条。

数据来源为旧系统迁移的「词 → 含义」记录（``jargon`` 表，``status = 'confirmed'``）。
本模块负责查询与命中，结果供提示词渲染为注入块；每次注入仅包含打分靠前的
:data:`MAX_INJECTED_JARGON` 条，不整表注入。

约束：

- 匹配为纯子串包含（``term in text``），无分词、无 bigram、无单字枚举。子串
  误报（「大」命中「大家」）不在匹配层拦截，由排序层压底：命中会话高频词表的
  词条获得量级领先的加分，未命中者仅剩自身计数，被条数上限过滤。高频词表收词
  要求 CJK ≥2 字，单字 CJK 黑话（咕、典、绷、孝）靠自身出现计数参与排序。
  单个 ASCII 字母或数字不参与匹配，见 :func:`_is_noise_term`。
- ``hits`` 命中即加一并记录，用于调优统计，不参与排序：按 hits 排序对误报词
  构成正反馈。
- 跨轮去重：同一个词在一段对话里只解释一次。已注入集合为进程内状态
  （:class:`InjectedTerms`），不落库，重启后为空。
- 只扫描他人消息（工作记忆中 ``role='user'`` 的消息与本轮批次原文），排除
  Bot 自身的发言。Bot 的名字与别名（含对用户的称呼）永不注入，见
  :func:`lookup_jargon` 的 ``protected_names``。
- 会话级 ``use`` 开关关闭时整条召回跳过（:func:`jargon_use_enabled`）；
  ``learn`` 开关键位预留，未实现。
- 查表按 ``(term, stream_id)``：本会话专属词条优先于全局
  （``stream_id IS NULL``）。
- 释义在注入前压缩到首句或 30 字（:func:`compress_meaning`）。截断只发生在
  本模块，``prompt.py`` 侧为纯渲染器，不做二次截断。

本模块不做自动学习、不做候选表、不做衰减。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Collection, Dict, List, Optional, Sequence, Tuple

from src.core.memory.high_frequency import strip_machine_spans
from src.core.observe.events import emit

# 单轮注入黑话词条数上限。
MAX_INJECTED_JARGON = 5

# 释义压缩上限（字符）。取首句（首个句末标点或换行之前），首句超长再硬截到
# 该上限；上限含结尾省略号，保证压缩结果整体不超过 30 字。
_MEANING_LIMIT = 30
_SENTENCE_ENDINGS = '。！？'

# 命中词条的展示形态：(词, 含义)，与 jargon 表的 term/meaning 列一一对应。
JargonEntry = Tuple[str, str]


def compress_meaning(meaning: str) -> str:
    """把黑话释义压缩到首句或 30 字以内。

    迁移释义平均约 271 字、多为百科体，原样注入占用提示词预算过大，
    通常首句已足以传达含义。

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


# 已注入词条的残留时长（毫秒），近似一段对话的跨度。
INJECTED_TTL_MS = 30 * 60 * 1000


def jargon_use_enabled(db: sqlite3.Connection, stream_id: int) -> bool:
    """读取会话级的黑话 use 开关。

    开关落在 ``meta`` 表的 ``jargon:use:{stream_id}`` 键上，缺省为开，
    仅在关闭时写行。``learn`` 开关的键位（``jargon:learn:{stream_id}``）
    预留未实现。

    :param db: 进程级 SQLite 连接。
    :param stream_id: 会话 ID。
    :return: 开关状态，缺省 ``True``。
    :raises sqlite3.Error: 读 ``meta`` 失败时抛出。
    副作用：无。
    """
    row = db.execute(
        'SELECT value FROM meta WHERE key = ?',
        (f'jargon:use:{stream_id}',),
    ).fetchone()
    return row is None or str(row['value']) != '0'


def set_jargon_use(db: sqlite3.Connection, stream_id: int, enabled: bool) -> bool:
    """写会话级的黑话 use 开关。

    :param db: 进程级 SQLite 连接。
    :param stream_id: 会话 ID。
    :param enabled: 目标状态；写 ``True`` 时直接删键回到缺省，不为每个
        会话留一行 ``'1'``。
    :return: 写入后的实际状态。
    :raises sqlite3.Error: 写 ``meta`` 失败时抛出。
    副作用：删除或写入 ``meta`` 行并提交。
    """
    key = f'jargon:use:{stream_id}'
    with db:
        if enabled:
            db.execute('DELETE FROM meta WHERE key = ?', (key,))
        else:
            db.execute(
                '''INSERT INTO meta (key, value) VALUES (?, '0')
                   ON CONFLICT(key) DO UPDATE SET value = '0' ''',
                (key,),
            )
    return jargon_use_enabled(db, stream_id)


class InjectedTerms:
    """进程内的会话级已注入词条集合：同一个词在一段对话里只解释一次。

    不落库：残留状态仅在本进程内有意义，重启后重建为空。落库会使重启后的
    Bot 仍携带上一段对话的解释记录。
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
        副作用：同时清理全表过期项，不写库。
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


# 命中会话高频词的基础加分。取值须压倒词条自身的出现计数（工作记忆窗口内
# 为十位数量级），故取千级。
_HIGH_FREQUENCY_BASE_BONUS = 1000.0
# 首次命中位置的惩罚系数：越早出现越相关；量级仅作同分排序微调，
# 须远小于单次出现计数。
_POSITION_PENALTY = 0.01
# 单条消息内同一个词的计数上限：与高频词表统计的防失真规则一致，
# 防止粘贴长文或机器段抬高单个词的自身计数。
_PER_MESSAGE_CAP = 3


def _is_noise_term(key: str) -> bool:
    """判断词条是否为单个 ASCII 字母或数字。

    单字 CJK 词条保留（咕、典、绷、孝为真实的黑话形态），其误报由排序层压底
    处理；单个 ASCII 字母或数字不参与匹配，迁移数据中的 ``a`` ``0`` 之类词条
    为抽取产物。

    - 现象：此类词条在大小写不敏感的子串匹配下命中几乎所有含英文或数字的
      消息，占全部命中的相当比例。
    - 原因：压底机制依赖会话高频词表，收词要求长度 ≥2，单个 ASCII 字符既
      无法获得加分，也没有自身出现计数可用，持续占用候选位。
    - 后果：不在匹配层排除，则只能人工逐条降级词表，且新数据会持续再产生。

    :param key: 已归一化（去空白、转小写）的词条。
    :return: 为真表示该词条不参与匹配。
    """
    return len(key) == 1 and key.isascii() and key.isalnum()


def _rank_matches(
    matches: Dict[str, _Match],
    high_frequency: Dict[str, Tuple[int, int]],
) -> List[Tuple[str, _Match]]:
    """按打分给命中词条排序。

    计分式：

    ``score = 词条自身出现次数 + 高频词加分 - 首次命中的消息位置 * 0.01``

    其中 ``高频词加分 = 1000 + 该词在本会话的出现次数 * 2 + max(0, 100 - 排名)``，
    仅当词条命中该会话的高频词表时计入。命中高频词表的词条获得千级加分；
    未命中者仅剩自身计数，由条数上限过滤。

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
    按 :func:`_rank_matches` 的计分式打分排序（同分长词优先）→ 去重登记
    → 截断到 :data:`MAX_INJECTED_JARGON` 条 → 压缩释义。

    :param db: 进程级 SQLite 连接（与 MemoryStore 同一来源）。
    :param stream_id: 当前会话 ID；专属词条只在该会话生效。
    :param scan_texts: 他人消息文本，时间顺序（历史在前、本轮批次原文在末尾）；
        只命中这里出现过的词。
    :param protected_names: 永不作为黑话注入的名字（bot 名、别名与对用户的
        称呼）；按归一化后的词面比对，不受词条 status 与作用域影响。
    :param injected: 会话级已注入集合；省略时不做跨轮去重。
    :param now: 当前毫秒时间戳；省略时取系统时钟（仅供去重残留判定）。
    :return: 至多 5 个 ``(词, 含义)`` 二元组，含义已压缩；无命中或该会话
        关闭 use 开关时为空列表。
    :raises sqlite3.Error: 查询或写入 hits 失败时抛出。
    副作用：
        对命中的词条执行 ``hits = hits + 1`` 并提交——含被去重排除、被上限
        截掉、没进提示词的那些（命中发生在查表，截断发生在注入）；向
        ``injected`` 登记真正注入的词条。
    """
    # 会话级 use 开关：关掉后整条召回静默跳过，hits 与去重登记都不发生。
    if not jargon_use_enabled(db, stream_id):
        return []
    # 机器段（图片/表情包占位、引用头）统计前剥掉，与高频词表共用同一清洗；
    # 否则占位文本会抬高相应词条的自身计数。
    texts = [
        strip_machine_spans(text.strip().lower())
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
        # Bot 自己的名字与别名不是黑话：任何 status、任何路径下都不得注入。
        if not key or key in guarded:
            continue
        if _is_noise_term(key):
            continue
        count = 0
        first_index = len(texts)
        for index, raw in enumerate(texts):
            occurrences = min(raw.count(key), _PER_MESSAGE_CAP)
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
    entries = [
        (match.row['term'].strip(), compress_meaning(match.row['meaning']))
        for _, match in selected
    ]
    if entries:
        # 仅在有命中时发事件：账本记录注入内容与质量，无命中的回合无信息量。
        # 控制台呈现由既有分层判据决定，此处不做控制台格式化。
        emit(
            'jargon_hit',
            candidates=len(matched),
            injected=len(entries),
            highFrequencyHits=sum(
                1 for term, _ in selected if term in high_frequency),
            truncated=len(fresh) - len(entries),
            chars=sum(len(term) + len(meaning) for term, meaning in entries),
            terms=[term for term, _ in entries],
        )
    return entries
