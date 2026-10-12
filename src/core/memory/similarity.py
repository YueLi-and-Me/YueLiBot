"""使用字符集合、相邻二元组与数字比对判断两条事实是否表达同一内容。

字符 Jaccard 衡量词面重合，bigram Jaccard 约束局部顺序；只有两项同时达到阈值
才允许事实合并，从而降低语序相反或仅共享少量字符的句子被错误去重的概率。

双阈值通过后还要求两句的差异不落在数字上：

- 现象：「2024年」改成「2026年」、「两只猫」改成「三只猫」的两句会被判为同一件事。
- 原因：bigram 只在连续 CJK 片段内生成，阿拉伯数字对它完全不可见；字符集合里
  一个数字的变化最多影响两个元素，句子稍长就仍在阈值之上。
- 后果：``MemoryStore.add_fact`` 会把更正后的新正文并入旧行并强化旧行，显式取代
  因新旧是同一行而落空；人工取代被 ``curate.replace_fact`` 以「同一件事」拒绝。

被 ``MemoryStore.add_fact`` 的相似去重与 ``curate.replace_fact`` 的同义拒绝调用；
``exact_key`` 另供 facts 与 knowledge 的严格去重键使用。
"""

from __future__ import annotations

from difflib import SequenceMatcher

import re

from .tokenize import bigrams

CHAR_THRESHOLD = 0.85
BIGRAM_THRESHOLD = 0.55

# 中文数词用字，只用于判断差异是否落在数字上，不做数值换算。
_CN_NUMERALS = frozenset('零〇一二两三四五六七八九十百千万亿')


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """计算两个集合的 Jaccard 相似度。

    :param a: 第一个字符串集合。
    :param b: 第二个字符串集合。
    :return: 交集大小除以并集大小；两个集合都为空时返回 1.0。
    副作用：不修改输入集合。
    """
    if not a and not b:
        return 1.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def _strip_separator(match: re.Match[str]) -> str:
    """删除一段标点与符号；夹在两个数字之间时收成一个「.」。"""
    text = match.string
    start, end = match.span()
    if 0 < start and end < len(text) and text[start - 1].isdecimal() and text[end].isdecimal():
        return '.'
    return ''


def _normalize(text: str) -> str:
    """保留字母、数字和 CJK 字符，并将英文转换为小写。

    stdlib ``re`` 不支持 ``\\p{P}`` 这类 Unicode 类别（写了会在编译期
    ``re.error``），这里用补集字符类达到「去掉标点与符号」的效果，
    不引入额外正则依赖。

    夹在两个数字之间的标点不删，统一收成一个「.」：
    「0.1.5」与「0.15」、「1.5万」与「15万」是不同的数，整体删去会生成同一个
    去重键，被 ``UNIQUE(person_id, content_key)`` 当成同一行。各种分隔写法
    （「2024-10-12」「2024/10/12」）收成同一个标记后仍视为相同。

    :param text: 待归一化的事实文本。

    :return: 删除其他字符后的文本。

    :raises TypeError: ``text`` 不是字符串时抛出。
    """
    return re.sub(r'[^\w一-鿿㐀-䶿豈-﫿]+', _strip_separator,
                  text, flags=re.UNICODE).lower()


def exact_key(text: str) -> str:
    """生成事实严格去重使用的归一化键。

    :param text: 原始事实文本。

    :return: ``_normalize(text)`` 的结果；键相同时表示归一化后字面完全重复。

    :raises TypeError: ``text`` 不是字符串时由归一化函数抛出。
    副作用：键的口径落在 facts 与 knowledge 的 ``content_key`` 列上，口径改动必须
        配一次迁移重算存量键（见 ``migrations/v33_to_v34.py``）。
    """
    return _normalize(text)


def _arabic_numbers(text: str) -> list[int]:
    """取出文本中每段连续阿拉伯数字的数值，排序后返回。"""
    return sorted(int(run) for run in re.findall(r'\d+', text))


def _has_cn_numeral(text: str) -> bool:
    """判断文本中是否含中文数词用字。"""
    return any(ch in _CN_NUMERALS for ch in text)


def _numbers_differ(na: str, nb: str) -> bool:
    """判断两段归一化文本在数字上是否有出入。

    阿拉伯数字与中文数词分开判定，满足任一条件即视为有出入：

    - 阿拉伯数字：比较数值的多重集。按多重集而非位置比较，使分句调换顺序、数字前后
      增删「的」「一直」这类同义改写不受影响；代价是数值不变、只交换位置的改动
      （「9月12日」与「12月9日」）不判为出入。
    - 中文数词：逐段检查 ``SequenceMatcher`` 的差异段，一侧差异段含数词、另一侧差异段
      连同两端紧邻字符也含数词时成立，即改动发生在某个数的位置上，如「两只」改「三只」、
      「十点」改「十一点」、「二十五」改「二十多」。中文数词用字也出现在「一直」「一起」
      「唯一」等普通词里，单纯增删这类词不算。

    :param na: 第一条事实 ``_normalize`` 后的文本。
    :param nb: 第二条事实 ``_normalize`` 后的文本。
    :return: 数字有出入时返回 ``True``；两段文本相同时返回 ``False``。
    :performance: ``SequenceMatcher`` 最坏为平方复杂度，只在双阈值已通过时调用；
        关闭 ``autojunk`` 使 200 字以上的长句也逐字比对，判定口径不随句长改变。
    """
    if _arabic_numbers(na) != _arabic_numbers(nb):
        return True
    matcher = SequenceMatcher(None, na, nb, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue
        left_near = na[max(i1 - 1, 0):i2 + 1]
        right_near = nb[max(j1 - 1, 0):j2 + 1]
        if (_has_cn_numeral(na[i1:i2]) and _has_cn_numeral(right_near)) or \
                (_has_cn_numeral(nb[j1:j2]) and _has_cn_numeral(left_near)):
            return True
    return False


def is_same_fact(a: str, b: str) -> bool:
    """判断两条文本是否表达同一件事，供事实去重与人工取代的同义拒绝使用。

    :param a: 第一条事实文本。
    :param b: 第二条事实文本。
    :return: 归一化文本相同时返回 ``True``；否则要求字符与 bigram 相似度同时达到
        阈值，且两句的差异不涉及数字（见 :func:`_numbers_differ`）。
    副作用：不修改输入文本。
    :performance: 双阈值判定与文本长度线性相关；数字比对只在双阈值通过后执行。
    """
    na = _normalize(a)
    nb = _normalize(b)
    if na == nb:
        return True
    char_sim = _jaccard(frozenset(na), frozenset(nb))
    if char_sim < CHAR_THRESHOLD:
        return False
    bigram_sim = _jaccard(frozenset(bigrams(na)), frozenset(bigrams(nb)))
    if bigram_sim < BIGRAM_THRESHOLD:
        return False
    return not _numbers_differ(na, nb)


def similarity(a: str, b: str) -> dict[str, float]:
    """计算两条文本的字符集合和 bigram 集合 Jaccard 相似度。

    :param a: 第一条待比较文本。
    :param b: 第二条待比较文本。

    :return: 包含 ``char`` 和 ``bigram`` 两个 ``[0, 1]`` 相似度值的字典。

    :raises TypeError: 输入不是字符串时由归一化函数抛出。
    """
    na = _normalize(a)
    nb = _normalize(b)
    return {
        'char': _jaccard(frozenset(na), frozenset(nb)),
        'bigram': _jaccard(frozenset(bigrams(na)), frozenset(bigrams(nb))),
    }
