"""
事实去重的相似度判定。直接移植自 src/core/memory/similarity.ts。

两个条件同时要求：
  · 字集 Jaccard 高 → 说的是同一批东西
  · bigram Jaccard 也不低 → 语序结构也接近

「他喜欢猫」和「猫喜欢他」字集相同但 bigram Jaccard 只有 0.2，
被干净地排除，避免语序相反的句子被误合。
"""

from __future__ import annotations

import re

from .tokenize import bigrams

CHAR_THRESHOLD = 0.85
BIGRAM_THRESHOLD = 0.55


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def normalize(text: str) -> str:
    """只留有意义字符：去掉标点、空白，英文统一小写。"""
    return re.sub(r'[\s\p{P}\p{S}]', '', text, flags=re.UNICODE).lower()


# Python 不支持 \p{P} in re without regex library; use a broader approach
def _normalize(text: str) -> str:
    """只留字母、数字、CJK，其余丢掉。"""
    return re.sub(r'[^\w一-鿿㐀-䶿豈-﫿]', '',
                  text, flags=re.UNICODE).lower()


def exact_key(text: str) -> str:
    """严格去重键：归一化后的原文。命中即字面完全重复。"""
    return _normalize(text)


def is_same_fact(a: str, b: str) -> bool:
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
    """供测试与调参用。"""
    na = _normalize(a)
    nb = _normalize(b)
    return {
        'char': _jaccard(frozenset(na), frozenset(nb)),
        'bigram': _jaccard(frozenset(bigrams(na)), frozenset(bigrams(nb))),
    }
