"""
中文检索分词（jieba 版）。

原 TS 侧使用 Intl.Segmenter + bigram 的唯一理由是「不能分发编译型 .dll」。
Python 侧 jieba 是纯 Python 包，这个约束消失了。

分词质量提升点（对比原实现）：
  Intl.Segmenter: 吃香菜 → 吃香/菜   ← 误分
  jieba:          吃香菜 → 吃/香菜   ← 正确

仍然保留 CJK bigram 兜底，原因：
  · 自造专名「月璃」不在 jieba 词典，bigram 产出「月璃」使 FTS 仍能召回
  · 新词、品牌名同理
  · bigram 检索是中文 IR 的公认可靠基线，代价极低
"""

from __future__ import annotations

import re

# jieba 首次 import 加载词典约 1 秒；
# 在模块顶层 import（lazy=False）使词典在进程启动时预热，
# 而不是在第一次用户查询时才卡住。
import jieba

# 关闭 jieba 的默认日志
jieba.setLogLevel(60)   # logging.CRITICAL

# 中日韩统一表意文字范围（同 TS 版）
_CJK_RE = re.compile(r'[㐀-䶿一-鿿豈-﫿]')

# 单字虚词（同 TS 版，略作扩充）
_STOP = frozenset(
    '的了和是在我你他她它们也就都而及与着'
    '过把被给让从到对为以之其于啊呀吗呢吧'
    '哦嗯这那有个不很会要说一'
)


def words(text: str) -> list[str]:
    """用 jieba 切词，过滤标点/空白/单字虚词。"""
    out: list[str] = []
    for token in jieba.cut(text, cut_all=False):
        w = token.strip().lower()
        if not w:
            continue
        # 非词元素（标点、空格）
        if not re.search(r'\w', w, re.UNICODE):
            continue
        # 单字 CJK 虚词
        if len(w) == 1 and _CJK_RE.match(w) and w in _STOP:
            continue
        out.append(w)
    return out


def bigrams(text: str) -> list[str]:
    """连续 CJK 片段的相邻二字组合（只对 CJK 字符做，英文不跨界）。"""
    out: list[str] = []
    run = ''

    def flush() -> None:
        for i in range(len(run) - 1):
            out.append(run[i:i + 2])

    for ch in text:
        if _CJK_RE.match(ch):
            run += ch
        else:
            flush()
            run = ''
    flush()
    return out


def index_tokens(text: str) -> str:
    """
    生成写入 FTS5 索引列的字符串：分词 + bigram，空格分隔。

    重复项保留：FTS5 的 BM25 需要词频信息，去重会让打分失真。
    """
    return ' '.join(words(text) + bigrams(text))


def match_query(text: str) -> str:
    """
    生成 FTS5 MATCH 查询串。

    用 OR 而不是 AND：记忆检索要的是「相关」，不是「全都命中」。
    用户问「上次说的那个香菜」，AND 会因为「上次」不在记忆里而全部落空。
    每个词加双引号转义，避免 FTS5 把内容当成语法（* NEAR - 等）。
    """
    terms: list[str] = list(dict.fromkeys(words(text) + bigrams(text)))  # 去重保序
    if not terms:
        return ''
    quoted = [f'"{t.replace(chr(34), chr(34) + chr(34))}"' for t in terms if t]
    return ' OR '.join(quoted)
