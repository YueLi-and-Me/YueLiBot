"""个性化 PageRank 纯函数的聚焦回归。"""

from __future__ import annotations

import math

import pytest

from src.core.memory import pagerank as pagerank_module


def test_personalized_pagerank_returns_normalized_steady_state() -> None:
    """稳态分布应归一化，并保留个性化种子的方向性。"""
    graph = {
        'seed': {'near': 3.0, 'far': 1.0},
        'near': {'seed': 1.0},
        'far': {'seed': 1.0},
    }

    scores = pagerank_module.personalized_pagerank(graph, {'seed': 2.0})

    assert math.isclose(sum(scores.values()), 1.0, abs_tol=1e-12)
    assert scores['seed'] > scores['near'] > scores['far']


def test_personalized_pagerank_rejects_invalid_weights() -> None:
    """负边权必须暴露，不能在归一化时被静默吞掉。"""
    with pytest.raises(ValueError, match='边权'):
        pagerank_module.personalized_pagerank({'a': {'b': -1.0}}, {'a': 1.0})


def test_personalized_pagerank_timeout_is_explicit(monkeypatch) -> None:
    """超时由专用异常报告，让调用层决定是否回退。"""
    ticks = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(pagerank_module, 'monotonic', lambda: next(ticks))

    with pytest.raises(pagerank_module.PageRankTimeoutError):
        pagerank_module.personalized_pagerank(
            {'a': {'b': 1.0}, 'b': {'a': 1.0}},
            {'a': 1.0},
            timeout_seconds=0.5,
        )
