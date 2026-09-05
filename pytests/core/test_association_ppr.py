"""联想召回改用两张独立 PPR 图后的契约回归。"""

from __future__ import annotations

import pickle
import sqlite3

from src.core.memory import association


_NOW = 1_800_000_000_000


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(':memory:')
    db.executescript(
        '''
        CREATE TABLE facts (
          id INTEGER PRIMARY KEY,
          content TEXT NOT NULL,
          strength REAL NOT NULL DEFAULT 1.0,
          updated_at INTEGER NOT NULL,
          half_life_hours REAL NOT NULL DEFAULT 720.0,
          active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE episodes (id INTEGER PRIMARY KEY, summary TEXT NOT NULL);
        CREATE TABLE knowledge (id INTEGER PRIMARY KEY, content TEXT NOT NULL);
        CREATE TABLE memory_nodes (
          id INTEGER PRIMARY KEY,
          ref_kind TEXT NOT NULL,
          ref_id INTEGER NOT NULL,
          UNIQUE(ref_kind, ref_id)
        );
        CREATE TABLE memory_edges (
          id INTEGER PRIMARY KEY,
          source_id INTEGER NOT NULL,
          target_id INTEGER NOT NULL,
          strength REAL NOT NULL,
          updated_at INTEGER NOT NULL,
          active INTEGER NOT NULL DEFAULT 1,
          UNIQUE(source_id, target_id)
        );
        CREATE TABLE knowledge_nodes (
          id INTEGER PRIMARY KEY,
          concept TEXT NOT NULL UNIQUE,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE knowledge_edges (
          id INTEGER PRIMARY KEY,
          source_id INTEGER NOT NULL,
          target_id INTEGER NOT NULL,
          strength REAL NOT NULL,
          updated_at INTEGER NOT NULL,
          UNIQUE(source_id, target_id)
        );
        '''
    )
    return db


def _fact(db: sqlite3.Connection, fact_id: int, content: str) -> None:
    db.execute(
        '''INSERT INTO facts
           (id, content, strength, updated_at, half_life_hours, active)
           VALUES (?, ?, 1.0, ?, 720.0, 1)''',
        (fact_id, content, _NOW),
    )


def _memory_node(
    db: sqlite3.Connection,
    node: int,
    ref_kind: str,
    ref_id: int,
) -> None:
    db.execute(
        'INSERT INTO memory_nodes (id, ref_kind, ref_id) VALUES (?, ?, ?)',
        (node, ref_kind, ref_id),
    )


def _memory_edge(
    db: sqlite3.Connection,
    source: int,
    target: int,
    strength: float,
) -> None:
    low, high = sorted((source, target))
    db.execute(
        '''INSERT INTO memory_edges
           (source_id, target_id, strength, updated_at, active)
           VALUES (?, ?, ?, ?, 1)''',
        (low, high, strength, _NOW),
    )


def test_hops_zero_is_byte_identical_to_legacy_spread() -> None:
    """★P-1：总开关关闭时不查询图，序列化结果也与旧实现相同。"""
    db = _db()
    _fact(db, 1, '种子')
    _memory_node(db, 101, 'fact', 1)
    db.commit()

    before = association._legacy_spread(db, [('fact', 1, 1.0)], _NOW, hops=0)
    after = association.spread(db, [('fact', 1, 1.0)], _NOW, hops=0)

    assert pickle.dumps(after) == pickle.dumps(before) == pickle.dumps([])


def test_out_degree_normalization_keeps_weak_hub_out_of_first_place() -> None:
    """★P-2：高度数但弱相关的节点不能仅凭度数压过强相关节点。"""
    db = _db()
    for fact_id in range(1, 24):
        _fact(db, fact_id, f'事实 {fact_id}')
    _memory_node(db, 100, 'fact', 1)
    _memory_node(db, 101, 'fact', 2)
    _memory_node(db, 102, 'fact', 3)
    _memory_edge(db, 100, 101, 0.99)
    _memory_edge(db, 100, 102, 0.01)
    for offset, fact_id in enumerate(range(4, 24), start=200):
        _memory_node(db, offset, 'fact', fact_id)
        _memory_edge(db, 102, offset, 1.0)
    db.commit()

    hits = association.spread(db, [('fact', 1, 1.0)], _NOW, hops=2, limit=1)

    assert [(hit.ref_kind, hit.ref_id) for hit in hits] == [('fact', 2)]


def test_ppr_timeout_falls_back_and_emits_trace(monkeypatch) -> None:
    """★P-3：任一 PPR 超时都回退完整旧结果，并留下可见事件。"""
    db = _db()
    _fact(db, 1, '种子')
    _fact(db, 2, '邻居')
    _memory_node(db, 101, 'fact', 1)
    _memory_node(db, 102, 'fact', 2)
    _memory_edge(db, 101, 102, 0.8)
    db.commit()
    expected = association._legacy_spread(db, [('fact', 1, 1.0)], _NOW)
    events = []

    def _timeout(*_args, **_kwargs):
        raise association.PageRankTimeoutError('测试超时')

    monkeypatch.setattr(association, 'personalized_pagerank', _timeout)
    monkeypatch.setattr(association.trace, 'emit', lambda kind, **fields: events.append((kind, fields)))

    actual = association.spread(db, [('fact', 1, 1.0)], _NOW)

    assert actual == expected
    assert events == [('memory_ppr_timeout', {'graph': 'memory'})]


def test_memory_and_knowledge_graphs_run_separately_then_fuse(monkeypatch) -> None:
    """★P-4：两套节点空间分别归一化、分别迭代，最后才融合命中。"""
    db = _db()
    _fact(db, 1, 'alpha')
    _fact(db, 2, 'memory only')
    _fact(db, 3, 'beta')
    _memory_node(db, 101, 'fact', 1)
    _memory_node(db, 102, 'fact', 2)
    _memory_node(db, 103, 'fact', 3)
    _memory_edge(db, 101, 102, 0.8)
    db.executemany(
        'INSERT INTO knowledge_nodes (id, concept, created_at) VALUES (?, ?, ?)',
        [(1, 'alpha', _NOW), (2, 'beta', _NOW)],
    )
    db.execute(
        '''INSERT INTO knowledge_edges
           (source_id, target_id, strength, updated_at)
           VALUES (1, 2, 261.0, ?)''',
        (_NOW,),
    )
    db.commit()

    calls = []
    real_pagerank = association.personalized_pagerank

    def _record(adjacency, personalization, **kwargs):
        snapshot = {node: dict(edges) for node, edges in adjacency.items()}
        calls.append((snapshot, dict(personalization)))
        return real_pagerank(adjacency, personalization, **kwargs)

    monkeypatch.setattr(association, 'personalized_pagerank', _record)

    hits = association.spread(db, [('fact', 1, 1.0)], _NOW, hops=1, limit=10)

    assert len(calls) == 2
    assert set(calls[0][0]) == {101, 102}
    assert set(calls[1][0]) == {1, 2}
    assert set(calls[0][0]).isdisjoint(calls[1][0])
    for outgoing in calls[1][0].values():
        assert abs(sum(outgoing.values()) - 1.0) < 1e-12
    assert {(hit.ref_kind, hit.ref_id) for hit in hits} == {
        ('fact', 2),
        ('fact', 3),
    }
