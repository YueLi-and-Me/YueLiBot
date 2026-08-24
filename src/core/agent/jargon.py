"""黑话查表与命中：本轮消息 → 分词 → ``jargon`` 表 → 命中词条。

群里的人用一堆只有那个群才懂的说法，她按字面理解、按字面回话，就明显是个
外人。旧系统积累了约 1024 条已确认的「词 → 含义」，由 [W2 迁移] 落进
``jargon`` 表；本模块只负责**用起来**：给出本轮消息里命中了哪些词，供提示词
渲染成一小段注入。**绝不整表倾倒**——真人也只是在别人用到某个词时才懂它，
1024 条全塞进提示词既浪费上下文，又会让她满嘴黑话。

设计要点（见 docs/memory-w3-jargon.md）：

- 分词复用 ``src.core.memory.tokenize``。在其之上补一层**字符级枚举**：单字
  黑话（咕、典、绷、孝）是群黑话的主力形态，而 jieba 会把「别咕了」并成
  「别咕」、「太典了」并成「太典」，``words`` 与 ``bigram`` 都拿不回单字；
  按 CJK 字符枚举是对 1 字词条的召回补充，与 bigram 之于被并掉的 2 字词
  同构，不是第二套切词。误报（如「字典」命中「典」）由「只查 confirmed」
  与单轮 5 条上限兜底，不会整段污染提示词。
- 查表按 ``(term, stream_id)``：先本会话专属、再全局（``stream_id IS NULL``），
  同一个词两边都有时本会话优先——同一个词在不同群含义可以不同，这正是
  ``stream_id`` 存在的理由。
- 只查 ``status = 'confirmed'``；pending 是只存不用的候选，永远不进提示词。
- 命中即 ``hits += 1``，用于后续调优；排序用加一**前**的值，本轮命中不参与
  本轮排序。
- 词典整表只有千级行且常驻页缓存，按 scope 取回后内存匹配比拼装 IN 列表
  更直接，也不受绑定参数个数上限牵制。

本模块不做自动学习、不做候选表、不做衰减：黑话是词典不是记忆，一个词的
含义不会因为三个月没人用就失效。
"""

from __future__ import annotations

import re
import sqlite3
from typing import List, Tuple

from src.core.memory.tokenize import bigrams, words

# 单轮注入黑话条数上限，本块唯一的常量：一轮命中十几个词说明要么分词太碎、
# 要么那批词条质量有问题，注入更多只会稀释真正相关的那几条。超过按 hits
# 降序取前 5。它只在这里执行——prompt 侧是纯渲染器，不做二次截断。
MAX_INJECTED_JARGON = 5

# 与 tokenize._CJK_RE 的 BMP 主区间同口径：枚举单字候选时只认表意文字，
# 不把标点、英文和数字当词条候选。刻意不导入私有名，两边若要改口径应一起改。
_CJK_CHAR_RE = re.compile(r'[一-鿿]')

# 命中词条的展示形态：(词, 含义)，与 jargon 表的 term/meaning 列一一对应。
JargonEntry = Tuple[str, str]


def _candidates(message_text: str) -> frozenset[str]:
    """生成本轮消息的词条候选集合。

    :param message_text: 本轮到达的消息原文（可多条拼接）。
    :return: 由 jieba 词、CJK bigram 和 CJK 单字组成、已转小写的候选集合。
    """
    return frozenset(
        words(message_text)
        + bigrams(message_text)
        + _CJK_CHAR_RE.findall(message_text)
    )


def lookup_jargon(
    db: sqlite3.Connection,
    stream_id: int,
    message_text: str,
) -> List[JargonEntry]:
    """查表并返回本轮消息命中的已确认黑话。

    流程：分词 → 查两个 scope 的 confirmed 词条 → 内存匹配 → 本会话优先
    去重 → 命中全部 ``hits + 1`` → 按加一前的 hits 降序（平局按词序稳定）
    截断到 :data:`MAX_INJECTED_JARGON` 条。

    :param db: 进程级 SQLite 连接（与 MemoryStore 同一来源）。
    :param stream_id: 当前会话 ID；专属词条只在该会话生效。
    :param message_text: 本轮到达的消息原文；只命中这里出现过的词。
    :return: 至多 5 个 ``(词, 含义)`` 二元组；无命中时为空列表。
    :raises sqlite3.Error: 查询或写入 hits 失败时抛出。
    副作用：
        对命中的词条执行 ``hits = hits + 1`` 并提交——含被上限截掉、
        没进提示词的那些（命中发生在查表，截断发生在注入）。
    """
    candidates = _candidates(message_text)
    if not candidates:
        return []
    rows = db.execute(
        '''SELECT id, term, meaning, hits, stream_id FROM jargon
           WHERE status = 'confirmed' AND (stream_id = ? OR stream_id IS NULL)''',
        (stream_id,),
    ).fetchall()
    matched: dict[str, sqlite3.Row] = {}
    for row in rows:
        key = row['term'].strip().lower()
        if key not in candidates:
            continue
        previous = matched.get(key)
        # 本会话优先：已有专属词条时全局让位；两个都是全局/专属时保留先见的。
        if previous is None or (previous['stream_id'] is None
                                and row['stream_id'] is not None):
            matched[key] = row
    if not matched:
        return []
    db.executemany(
        'UPDATE jargon SET hits = hits + 1 WHERE id = ?',
        [(row['id'],) for row in matched.values()],
    )
    db.commit()
    # 排序用读出来的旧 hits：本轮的 +1 不参与本轮排序。
    ranked = sorted(matched.values(), key=lambda row: (-row['hits'], row['term']))
    return [
        (row['term'].strip(), row['meaning'])
        for row in ranked[:MAX_INJECTED_JARGON]
    ]
