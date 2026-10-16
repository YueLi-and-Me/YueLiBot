"""recall 与 consult 的重排通道验收。

配置了重排时：候选按重排分数排序，分数不到 ``rerank_min_score`` 的不返回，一条不剩
就回「没想起来」；重排失败时整次检索报错，不退回融合分排序。未配置时的行为由既有
用例覆盖。
"""

from __future__ import annotations

from typing import Dict, List, Sequence

import pytest

from src.core.agent.cognition import CognitiveRequest, ConsultAction, RecallAction
from src.core.memory.knowledge import index_knowledge
from src.core.memory.similarity import exact_key
from src.core.memory.store import FactInput, MemoryStore
from src.core.memory.tuning import set_active_overrides
from src.core.observe.store import event_store

_NOW = 1_750_000_000_000
_OWNER = 1


def _scorer(by_text: Dict[str, float], seen: List[List[str]]):
    """按正文里的关键词给分；没对上的记 0。记下每次收到的候选正文。"""
    async def rerank(query: str, documents: Sequence[str]) -> List[float]:
        seen.append(list(documents))
        return [next((score for key, score in by_text.items() if key in doc), 0.0) for doc in documents]

    return rerank


def _recall_request(query: str) -> CognitiveRequest:
    return CognitiveRequest(
        action='recall', query=query, stream_id=1, stream_kind='direct',
        person_ids=(_OWNER,), message_watermark=0,
    )


def _consult_request(query: str) -> CognitiveRequest:
    return CognitiveRequest(
        action='consult', query=query, stream_id=1, stream_kind='group',
        person_ids=(_OWNER,), message_watermark=0,
    )


def _seed_facts(db, contents: List[str]) -> MemoryStore:
    store = MemoryStore(db)
    for content in contents:
        store.add_fact(_OWNER, FactInput(kind='偏好', content=content, origin_kind='direct'), _NOW)
    return store


def _seed_knowledge(db, contents: List[str]) -> None:
    MemoryStore(db)
    for content in contents:
        db.execute(
            'INSERT INTO knowledge (content, content_key, source, created_at) VALUES (?, ?, ?, ?)',
            (content, exact_key(content), 'test', _NOW),
        )
    db.commit()
    index_knowledge(db, limit=1000)


@pytest.fixture(autouse=True)
def _reset_overrides():
    set_active_overrides({})
    yield
    set_active_overrides({})


async def test_recall_keeps_only_scores_above_floor_in_score_order(db) -> None:
    store = _seed_facts(db, ['他喜欢显卡超频', '他买了一张新显卡', '他觉得显卡太贵'])
    seen: List[List[str]] = []
    action = RecallAction(
        store, lambda person, stream: '他', db,
        rerank=_scorer({'新显卡': 0.9, '太贵': 0.6, '超频': 0.2}, seen),
    )

    result = await action.execute(_recall_request('显卡'))

    assert len(seen) == 1 and len(seen[0]) == 3, '三条候选在一次重排请求里打分'
    assert result.hit_count == 2
    assert result.text.index('新显卡') < result.text.index('太贵'), '按重排分数降序'
    assert '超频' not in result.text, '低于默认下限 0.5 的不返回'
    event = event_store.search(kinds=['recall_rerank']).events[0]
    assert event['action'] == 'recall' and event['candidates'] == 3 and event['kept'] == 2
    assert event['floor'] == 0.5 and event['topScore'] == 0.9


async def test_recall_says_nothing_found_when_all_below_floor(db) -> None:
    store = _seed_facts(db, ['他喜欢显卡超频'])
    action = RecallAction(store, lambda person, stream: '他', db, rerank=_scorer({}, []))

    result = await action.execute(_recall_request('显卡'))

    assert result.hit_count == 0
    assert '没有想起' in result.text


async def test_recall_floor_follows_tuning_override(db) -> None:
    store = _seed_facts(db, ['他买了一张新显卡'])
    action = RecallAction(store, lambda person, stream: '他', db, rerank=_scorer({'新显卡': 0.6}, []))
    set_active_overrides({'rerank_min_score': 0.7})

    result = await action.execute(_recall_request('显卡'))

    assert result.hit_count == 0


async def test_recall_rerank_failure_propagates(db) -> None:
    """重排挂了要让这次检索报失败，不能悄悄换回不设下限的排序。"""
    store = _seed_facts(db, ['他买了一张新显卡'])

    async def broken(query: str, documents: Sequence[str]) -> List[float]:
        raise RuntimeError('重排服务不可用')

    action = RecallAction(store, lambda person, stream: '他', db, rerank=broken)

    with pytest.raises(RuntimeError, match='重排服务不可用'):
        await action.execute(_recall_request('显卡'))


async def test_consult_reranks_and_counts_only_shown_hits(db) -> None:
    _seed_knowledge(db, ['勾股定理：直角三角形两直角边的平方和等于斜边的平方', '勾股数是满足勾股定理的正整数组'])
    action = ConsultAction(db, rerank=_scorer({'勾股数': 0.8, '直角三角形': 0.3}, []))

    result = await action.execute(_consult_request('勾股'))

    assert result.hit_count == 1
    assert '勾股数' in result.text and '直角三角形' not in result.text
    counts = dict(db.execute('SELECT content, hit_count FROM knowledge'))
    assert counts['勾股数是满足勾股定理的正整数组'] == 1
    assert counts['勾股定理：直角三角形两直角边的平方和等于斜边的平方'] == 0, '被下限挡下的不记命中'


async def test_consult_rerank_failure_propagates(db) -> None:
    _seed_knowledge(db, ['勾股数是满足勾股定理的正整数组'])

    async def broken(query: str, documents: Sequence[str]) -> List[float]:
        raise RuntimeError('重排服务不可用')

    with pytest.raises(RuntimeError, match='重排服务不可用'):
        await ConsultAction(db, rerank=broken).execute(_consult_request('勾股'))
