"""提供中文检索分词、CJK bigram 和 FTS5 查询串构造。

jieba 负责常规中文切词，CJK bigram 作为专名和新词的召回补充；索引文本保留
重复项以提供词频信息，查询串使用 OR 和双引号避免 FTS5 语法注入。
"""

from __future__ import annotations

import re

# jieba 首次 import 加载词典约 1 秒；
# 在模块顶层 import（lazy=False）使词典在进程启动时预热，
# 而不是在第一次用户查询时才卡住。
import jieba

# 关闭 jieba 的默认日志
jieba.setLogLevel(60)   # logging.CRITICAL

# 中日韩统一表意文字范围，用于区分中文 bigram 与英文 token。
_CJK_RE = re.compile(r'[㐀-䶿一-鿿豈-﫿]')

# 单字虚词集合；过滤后可减少结构助词对召回排序的干扰。
_STOP = frozenset(
    '的了和是在我你他她它们也就都而及与着'
    '过把被给让从到对为以之其于啊呀吗呢吧'
    '哦嗯这那有个不很会要说一'
)


def words(text: str) -> list[str]:
    """使用 jieba 精确模式分词，并过滤标点、空白和单字虚词。

    Args:
        text: 待分词的中文或混合文本。

    Returns:
        按原文顺序排列、已转小写的有效 token 列表；保留重复 token 以提供词频信息。

    Raises:
        TypeError: ``text`` 不是可迭代字符串时由 jieba 操作抛出。

    Performance:
        首次调用可能触发 jieba 词典加载，之后耗时与文本长度相关。
    """
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
    """生成连续 CJK 片段的相邻二字组合，不跨越英文或非 CJK 字符。

    Args:
        text: 待生成 bigram 的文本。

    Returns:
        按原文顺序排列的二字组合列表；长度不足两个字符的片段不产生结果。

    Raises:
        TypeError: ``text`` 不是可迭代字符串时抛出。

    Performance:
        时间和临时空间复杂度与输入文本长度线性相关。
    """
    out: list[str] = []
    run = ''

    def flush() -> None:
        """把当前连续 CJK 片段拆成相邻二字组合并追加到结果。

        :side_effects: 读取外层 `run` 字符串并修改 `out` 列表，不清空 `run`。
        """
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
    """生成写入 FTS5 索引列的分词和 bigram 字符串。

    重复 token 必须保留，因为 FTS5 的 BM25 使用词频参与评分，去重会改变相关度。

    Args:
        text: 待建立索引的事实或线索文本。

    Returns:
        由 jieba token 和 CJK bigram 按空格拼接的索引文本。

    Raises:
        TypeError: ``text`` 不是字符串时由分词逻辑抛出。

    Performance:
        处理时间与输入文本长度线性相关；首次调用可能包含 jieba 词典加载成本。
    """
    return ' '.join(words(text) + bigrams(text))


def match_query(text: str) -> str:
    """生成经过引号转义的 FTS5 MATCH 查询串。

    查询词使用 OR 连接，以允许部分相关词命中；每个词使用双引号转义，避免内容被
    FTS5 解析为 ``*``、``NEAR`` 或 ``-`` 等查询语法。

    Args:
        text: 待检索的自然语言查询文本。

    Returns:
        由去重 token 组成的 FTS5 MATCH 表达式；没有有效 token 时返回空字符串。

    Raises:
        TypeError: ``text`` 不是字符串时由分词逻辑抛出。
    """
    terms: list[str] = list(dict.fromkeys(words(text) + bigrams(text)))  # 去重保序
    if not terms:
        return ''
    quoted = [f'"{t.replace(chr(34), chr(34) + chr(34))}"' for t in terms if t]
    return ' OR '.join(quoted)
