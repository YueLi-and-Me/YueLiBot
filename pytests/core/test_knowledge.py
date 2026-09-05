"""W4 知识检索验收。

对应 开发文档 memory-w4-knowledge.md（不随代码分发） 第三节与第五节：
- ★W4-3：向量服务整批失败（embedding 留 NULL）时 search_knowledge 仍能靠
  BM25 返回结果，不抛异常、不中断调用方；
- ★W4-4：knowledge_fts 的 rowid 与 knowledge.id 对齐——按第二条的关键词检索，
  返回的必须是第二条；
- BM25 + 向量联合打分复用 decay.py 的 relevance_from_bm25() 与 score()，
  知识无衰减（retention 恒取 1）；related_concepts 一跳按 strength 排序；
  touch_knowledge 只做命中计数，不参与打分。
"""

from __future__ import annotations

import sqlite3
import struct

from src.core.memory.knowledge import (
    index_knowledge,
    related_concepts,
    search_knowledge,
    touch_knowledge,
)
from src.core.memory.similarity import exact_key
from src.core.memory.store import MemoryStore

_NOW = 1_750_000_000_000


def _seed(db: sqlite3.Connection, contents: list[str]) -> list[int]:
    """播种知识行并补建 FTS，返回 id 列表。"""
    ids = []
    for content in contents:
        cur = db.execute(
            'INSERT INTO knowledge (content, content_key, source, created_at)'
            ' VALUES (?, ?, ?, ?)',
            (content, exact_key(content), 'migrate-m4', _NOW),
        )
        ids.append(cur.lastrowid)
    db.commit()
    index_knowledge(db, limit=1000)
    return ids


def _vec(*values: float) -> bytes:
    return struct.pack(f'{len(values)}f', *values)


def test_search_hits_the_row_its_tokens_belong_to(db) -> None:
    """★W4-4：两条内容不同的知识，按第二条的关键词检索，返回的必须是第二条。"""
    MemoryStore(db)
    ids = _seed(db, ['光年之外的殖民船仍在航行', '幻影坦克擅长光学迷彩伏击'])

    hits = search_knowledge(db, '光学迷彩', 5)

    assert [h.id for h in hits] == [ids[1]]
    assert '幻影坦克' in hits[0].content


def test_search_returns_results_when_vectors_missing(db) -> None:
    """★W4-3：向量重算整批失败（embedding 留 NULL）时，带查询向量检索仍走 BM25。"""
    MemoryStore(db)
    _seed(db, ['勾股定理：直角三角形两直角边的平方和等于斜边的平方'])
    query_embedding = _vec(1.0, 0.0, 0.0, 0.0)

    hits = search_knowledge(db, '勾股定理', 5, query_embedding=query_embedding)

    assert len(hits) == 1, 'embedding 为 NULL 不允许拖垮 BM25 召回'
    assert hits[0].score > 0


def test_search_tolerates_corrupt_embedding(db) -> None:
    """单条坏向量不阻断整批召回：维度对不上时保留词面相关度。"""
    MemoryStore(db)
    ids = _seed(db, ['折射率匹配是光学迷彩的原理'])
    db.execute('UPDATE knowledge SET embedding = ? WHERE id = ?', (b'\x00', ids[0]))
    db.commit()

    hits = search_knowledge(db, '折射率', 5, query_embedding=_vec(1.0, 0.0, 0.0, 0.0))

    assert [h.id for h in hits] == [ids[0]]


def test_vector_fusion_can_overrule_lexical_order(db) -> None:
    """联合打分真实生效：词面更优的 A 与语义更优的 B，给查询向量后 B 排到前面。"""
    MemoryStore(db)
    ids = _seed(db, ['迷彩迷彩迷彩，满屏都是迷彩', '迷彩涂层与光学折射原理'])
    query_embedding = _vec(1.0, 0.0, 0.0, 0.0)
    # A 的向量与查询相反（余弦 -1），B 的向量与查询相同（余弦 1）。
    db.execute('UPDATE knowledge SET embedding = ? WHERE id = ?', (_vec(-1.0, 0.0, 0.0, 0.0), ids[0]))
    db.execute('UPDATE knowledge SET embedding = ? WHERE id = ?', (_vec(1.0, 0.0, 0.0, 0.0), ids[1]))
    db.commit()

    lexical = search_knowledge(db, '迷彩', 5)
    fused = search_knowledge(db, '迷彩', 5, query_embedding=query_embedding)

    assert lexical[0].id == ids[0], '前置条件：词面上 A 必须更优，否则这个用例什么也没证明'
    assert fused[0].id == ids[1], '向量融合后语义更优的 B 必须反超'
    assert all(0.0 <= h.score <= 1.0 for h in fused)


def test_search_rejects_empty_query(db) -> None:
    """无有效检索词时返回空列表，不让 FTS 收到空 MATCH。"""
    MemoryStore(db)
    _seed(db, ['任何东西'])
    assert search_knowledge(db, '的了和', 5) == []
    assert search_knowledge(db, '', 5) == []


def test_related_concepts_walks_one_hop_ordered_by_strength(db) -> None:
    """沿 knowledge_edges 走一跳，两个方向都算相关，按 strength 降序。"""
    MemoryStore(db)
    now = _NOW
    for concept in ('幻影坦克', '天启坦克', '光棱塔', '磁暴线圈'):
        db.execute('INSERT INTO knowledge_nodes (concept, created_at) VALUES (?, ?)',
                   (concept, now))
    node = {r[1]: r[0] for r in db.execute('SELECT id, concept FROM knowledge_nodes')}
    edges = [
        ('幻影坦克', '天启坦克', 18.0),
        ('光棱塔', '幻影坦克', 200.0),   # 反向边同样算相关
        ('幻影坦克', '磁暴线圈', 5.0),
        ('天启坦克', '光棱塔', 99.0),    # 与幻影坦克无关，不许出现（那是两跳）
    ]
    for source, target, strength in edges:
        db.execute(
            'INSERT INTO knowledge_edges (source_id, target_id, strength, updated_at)'
            ' VALUES (?, ?, ?, ?)',
            (node[source], node[target], strength, now),
        )
    db.commit()

    related = related_concepts(db, '幻影坦克', 10)
    assert related == ['光棱塔', '天启坦克', '磁暴线圈']
    assert related_concepts(db, '幻影坦克', 2) == ['光棱塔', '天启坦克']
    assert related_concepts(db, '不存在的概念', 10) == []
    assert related_concepts(db, '  ', 10) == []


def test_touch_knowledge_counts_hits(db) -> None:
    """命中计数：hit_count 累加、last_hit_at 更新；不影响任何打分字段。"""
    MemoryStore(db)
    ids = _seed(db, ['第一条知识', '第二条知识'])

    touch_knowledge(db, [ids[0], ids[1], ids[0]], now=_NOW)
    touch_knowledge(db, [ids[0]], now=_NOW + 1000)

    rows = {
        r[0]: (r[1], r[2])
        for r in db.execute('SELECT id, hit_count, last_hit_at FROM knowledge')
    }
    assert rows[ids[0]] == (2, _NOW + 1000)
    assert rows[ids[1]] == (1, _NOW), '同一次调用里的重复 id 只计一次'

    before = db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0]
    touch_knowledge(db, [], now=_NOW)
    touch_knowledge(db, [99999], now=_NOW)
    assert db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0] == before
