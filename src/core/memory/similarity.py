"""使用字符集合和相邻二元组判断两条事实是否表达同一内容。

字符 Jaccard 衡量词面重合，bigram Jaccard 约束局部顺序；只有两项同时达到阈值
才允许事实合并，从而降低语序相反或仅共享少量字符的句子被错误去重的概率。
"""

from __future__ import annotations

import re

from .tokenize import bigrams

CHAR_THRESHOLD = 0.85
BIGRAM_THRESHOLD = 0.55


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


def normalize(text: str) -> str:
    """使用当前正则规则移除标点和空白，并将英文转换为小写。

    :param text: 待归一化的文本。

    :return: 删除匹配字符并转为小写后的文本。

    :raises re.error: 当前 Python 正则引擎不支持配置的字符类别时抛出。
    :raises TypeError: ``text`` 不是字符串时抛出。
    """
    return re.sub(r'[\s\p{P}\p{S}]', '', text, flags=re.UNICODE).lower()


# Python 不支持 \p{P} in re without regex library; use a broader approach
def _normalize(text: str) -> str:
    """保留字母、数字和 CJK 字符，并将英文转换为小写。

    :param text: 待归一化的事实文本。

    :return: 删除其他字符后的文本。

    :raises TypeError: ``text`` 不是字符串时抛出。
    """
    return re.sub(r'[^\w一-鿿㐀-䶿豈-﫿]', '',
                  text, flags=re.UNICODE).lower()


def exact_key(text: str) -> str:
    """生成事实严格去重使用的归一化键。

    :param text: 原始事实文本。

    :return: ``_normalize(text)`` 的结果；键相同时表示归一化后字面完全重复。

    :raises TypeError: ``text`` 不是字符串时由归一化函数抛出。
    """
    return _normalize(text)


def is_same_fact(a: str, b: str) -> bool:
    """判断两条文本是否满足事实去重的双阈值条件。

    :param a: 第一条事实文本。
    :param b: 第二条事实文本。
    :return: 归一化文本相同，或字符与 bigram 相似度同时达到阈值时返回 `True`。
    副作用：不修改输入文本。
    :performance: 复杂度与两条文本归一化后的长度线性相关。
    """
    na = _normalize(a)
    nb = _normalize(b)
    if na == nb:
        return True
    char_sim = _jaccard(frozenset(na), frozenset(nb))
    if char_sim < CHAR_THRESHOLD:
        return False
    bigram_sim = _jaccard(frozenset(bigrams(na)), frozenset(bigrams(nb)))
    return bigram_sim >= BIGRAM_THRESHOLD


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
