"""会话高频词表：按会话统计 ``messages`` 里其他人真正在用的词。

黑话召回的打分用它做碾压性加分——命中的词条浮上来，没命中的误报沉底。
因此这张表必须像「这个群真实的高频词」，而不是通用词表或机器文本的词频：

- 平台占位段（``[表情包：…]``、``[图片：…]``、``[回复 …]`` 等）是管线生成的，
  不是人打的字，统计前剥离；
- 单条粘贴文本（整篇文章、整段代码）会把某个词刷到上百次，逐条出现次数
  封顶，词频要反映「很多人在说」而不是「有人贴了长文」。

对外暴露 :func:`collect_terms`（纯函数，文本列表 → 排名词表）与
:func:`rebuild_stream_terms`（查库、统计、整表重写一个会话的快照）。
后台任务与测试共用后者。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Dict, List, Sequence, Tuple

from src.core.memory.tokenize import words

# 平台渲染进消息正文的机器段：表情/图片/语音等占位与引用回复头。它们在每条
# 消息里重复出现（「表情包」「主题」由此霸榜），不剥离的话整张表都是管线词频。
# 重复替换以拆掉引用头里的嵌套占位（``[回复 某人：[表情包]]``）。
_PLACEHOLDER_SPAN = re.compile(r'\[(?:表情|表情包|图片|语音|视频|文件|回复|引用)[^\]]*\]')

# 纯数字与数字符号混合的 token（时间戳、编号）不构成「群里的说法」。
# 与 tokenize._CJK_RE 同口径的 BMP 主区间，不导入私有名；两边若要改口径应一起改。
_HAS_WORD_CHAR = re.compile(r'[A-Za-z㐀-䶿一-鿿豈-﫿]')

# jieba 的切词会给出大量二字虚词；``tokenize.words`` 的停用词表只覆盖单字。
# 不再滤掉它们，高频词表的前排会全是「这个/什么/一个」，看不出这个群在聊
# 什么——那正是收词失败的形状。清单刻意只收功能词，不收任何可能成为黑话
# 的实词。
_FUNCTION_WORDS = frozenset({
    '这个', '那个', '这些', '那些', '一个', '一些', '一下', '一边', '一般',
    '什么', '怎么', '为什么', '怎么样', '怎么办', '那么', '这么', '多少',
    '不是', '没有', '还有', '就是', '还是', '但是', '可是', '而且', '然后',
    '已经', '应该', '可能', '可以', '能够', '必须', '需要', '觉得', '知道',
    '现在', '刚才', '马上', '正在', '准备', '时候', '地方', '东西', '事情',
    '问题', '样子', '感觉', '意思', '情况', '其实', '当然', '真的', '确实',
    '反正', '总是', '经常', '一直', '起来', '出来', '过去', '回来', '开始',
    '你们', '我们', '他们', '她们', '自己', '别人', '大家', '这里', '那里',
    '的话', '只有', '所有', '以及', '或者', '如果', '因为', '所以',
    '虽然', '不过', '直接', '稍微', '根本', '一定', '完全', '继续', '说的',
    'the', 'and', 'you', 'for', 'that', 'this', 'with', 'have', 'just',
    'like', 'not', 'are', 'but', 'can', 'all', 'was', 'out', 'ok', 'no',
})


def collect_terms(
    texts: Sequence[str],
    *,
    limit: int = 100,
    per_message_cap: int = 3,
) -> List[Tuple[str, int, int]]:
    """从一组消息文本统计高频词。

    收词规则（整套黑话打分方案的支点，写死在此处）：**CJK 词必须 ≥2 字，
    拉丁与其他 ≥2 字符**。这一条不能放宽——单字永远进不了高频词表，才
    拿不到召回打分里那 1000 分量级的加分，于是「大」「开」这类单字误报
    只剩自身的个位数计数、被条数上限截光；同时真单字黑话（咕、典、绷、
    孝）不被整批降级，若它在这个群真的高频，自然会作为词条自身计数浮
    上来。放宽到单字入库，「了」「的」「大」会瞬间霸榜，打分沉底机制
    整体失效。

    :param texts: 已剥离机器段与否皆可的消息文本，时间顺序。
    :param limit: 落表条数上限；与打分公式 ``max(0, 100 - rank)`` 的量程
        对齐，超出名次的额外分恰为零。
    :param per_message_cap: 单条消息内同一个词的计数上限，防粘贴长文
        劫持词频。
    :return: ``(词, 出现次数, 覆盖消息数)`` 元组列表，按
        出现次数降序 → 消息数降序 → 词长降序 → 词面 排序。
    :raises TypeError: ``texts`` 里的项不是字符串时由分词逻辑抛出。
    副作用：无。
    """
    occurrences: Dict[str, int] = {}
    messages: Dict[str, int] = {}
    for raw in texts:
        text = raw or ''
        previous = None
        while previous != text:
            previous = text
            text = _PLACEHOLDER_SPAN.sub(' ', text)
        tokens = [
            token for token in words(text)
            # 收词下限：CJK ≥2 字、拉丁与其他 ≥2 字符，理由见函数 docstring。
            if len(token) >= 2
            and token not in _FUNCTION_WORDS
            and _HAS_WORD_CHAR.search(token)
        ]
        per_message: Dict[str, int] = {}
        for token in tokens:
            per_message[token] = per_message.get(token, 0) + 1
        for token, count in per_message.items():
            occurrences[token] = occurrences.get(token, 0) + min(count, per_message_cap)
            messages[token] = messages.get(token, 0) + 1
    ranked = sorted(
        occurrences,
        key=lambda term: (
            -occurrences[term],
            -messages[term],
            -len(term),
            term,
        ),
    )
    return [
        (term, occurrences[term], messages[term])
        for term in ranked[:limit]
    ]


def rebuild_stream_terms(
    db: sqlite3.Connection,
    stream_id: int,
    *,
    now: int,
    window_ms: int = 7 * 24 * 60 * 60 * 1000,
    limit: int = 100,
) -> int:
    """重建一个会话的高频词表快照。

    只统计 ``role = 'user'`` 的消息：这张表表达「其他人在说什么」，bot 自己
    的发言不该反过来喂给理解他人说法的打分。整表按会话全量重写（旧词不再
    高频时直接消失），统计完全在后台旁路进行，不进回合关键路径。

    :param db: 进程级 SQLite 连接（与 MemoryStore 同一来源）。
    :param stream_id: 目标会话 ID。
    :param now: 当前毫秒时间戳，窗口右端。
    :param window_ms: 统计窗口长度，默认 7 天：太短会让夜聊话题独占榜单，
        太长则季节性梗下不去。
    :param limit: 落表条数上限。
    :return: 本次落表的词条数。
    :raises sqlite3.Error: 查询或重写失败时抛出。
    副作用：
        删除并重写 ``high_frequency_terms`` 中该会话的全部行后提交；
        统计窗口内没有用户消息时会清空该会话的旧行。
    """
    rows = db.execute(
        '''SELECT content FROM messages
           WHERE stream_id = ? AND role = 'user' AND created_at >= ?''',
        (stream_id, now - window_ms),
    ).fetchall()
    ranked = collect_terms([str(row['content']) for row in rows], limit=limit)
    with db:
        db.execute(
            'DELETE FROM high_frequency_terms WHERE stream_id = ?', (stream_id,))
        db.executemany(
            '''INSERT INTO high_frequency_terms
               (stream_id, term, occurrence_count, message_count, rank, built_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            [
                (stream_id, term, occurrence, message_count, index, now)
                for index, (term, occurrence, message_count)
                in enumerate(ranked, start=1)
            ],
        )
    return len(ranked)
