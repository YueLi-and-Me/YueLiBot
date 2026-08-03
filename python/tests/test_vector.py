"""
向量召回测试。

不调真实 embedding API：用假向量验证 BM25+向量混合打分的逻辑，
以及无向量时纯 BM25 正常降级。
"""

from __future__ import annotations

import struct
import sqlite3
import pytest

from yueli.memory.store import EpisodeInput, FactInput, MemoryStore
from yueli.memory.embed import cosine, _pack, _unpack


def _make_vec(values: list[float]) -> bytes:
    """造一个归一化假向量。"""
    norm = sum(v * v for v in values) ** 0.5
    normalized = [v / norm for v in values]
    return _pack(normalized)


@pytest.fixture
def store():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    s = MemoryStore(db)
    yield s
    db.close()


class TestVectorRecall:
    def test_pure_bm25_unchanged(self, store):
        """无查询向量时，结果与原来的纯 BM25 路径一致（无 embedding 参数）。"""
        id1 = store.add_fact(FactInput(kind='偏好', content='玩家不喜欢吃香菜'), 0)
        store.add_fact(FactInput(kind='偏好', content='玩家养了一只猫'), 0)
        results = store.recall_facts('香菜', 5)
        assert len(results) > 0
        assert results[0].content == '玩家不喜欢吃香菜'

    def test_bm25_fallback_when_no_query_embedding(self, store):
        """query_embedding=None 时不使用向量，只用 BM25。"""
        store.add_fact(FactInput(kind='偏好', content='玩家喜欢 Rust 编程'), 0)
        results = store.recall_facts('Rust', 5, query_embedding=None)
        assert any('Rust' in r.content for r in results)

    def test_hybrid_score_fuses_bm25_and_vector(self, store):
        """有向量时，高余弦相似度事实的排名应高于低余弦的事实（BM25 匹配度相同时）。

        ★ 查询词故意选「玩家喜欢」—— 两条事实都含这些词，BM25 分值接近，
          向量相似度的差异才能真正影响排名。
          若查询词只匹配其中一条，另一条根本不进候选集，测的就不是混合打分了。
        """
        id_coding = store.add_fact(FactInput(kind='习惯', content='玩家喜欢写代码做项目'), 0)
        id_music = store.add_fact(FactInput(kind='习惯', content='玩家喜欢听音乐放松'), 0)

        # 归一化假向量：coding=[1,0], music=[0,1], query≈coding
        vec_coding = _make_vec([1.0, 0.0])
        vec_music = _make_vec([0.0, 1.0])
        query_vec = _make_vec([0.95, 0.05])

        store.store_embedding(id_coding, vec_coding)
        store.store_embedding(id_music, vec_music)

        results = store.recall_facts('玩家喜欢', 5, query_embedding=query_vec)
        ids = [r.id for r in results]
        assert id_coding in ids and id_music in ids, f'两条事实都应出现在召回结果里: {ids}'
        assert ids.index(id_coding) < ids.index(id_music), (
            f'向量更接近 coding 时，coding({id_coding}) 应排在 music({id_music}) 前面，实际: {ids}'
        )

    def test_partial_embedding_graceful(self, store):
        """部分事实有向量、部分没有，不应崩溃。"""
        id1 = store.add_fact(FactInput(kind='偏好', content='玩家喜欢猫'), 0)
        id2 = store.add_fact(FactInput(kind='偏好', content='玩家喜欢狗'), 0)
        # 只给第一条存向量
        store.store_embedding(id1, _make_vec([1.0, 0.0]))
        query_vec = _make_vec([1.0, 0.0])

        results = store.recall_facts('宠物 猫', 5, query_embedding=query_vec)
        assert len(results) > 0  # 不崩溃

    def test_cosine_math(self):
        """余弦函数本身的数学正确性。"""
        a = _make_vec([1.0, 0.0, 0.0])
        b = _make_vec([1.0, 0.0, 0.0])
        c = _make_vec([0.0, 1.0, 0.0])
        dim = 3
        assert abs(cosine(a, b, dim) - 1.0) < 1e-5   # 相同方向
        assert abs(cosine(a, c, dim)) < 1e-5          # 正交

    def test_facts_without_embedding(self, store):
        """facts_without_embedding 返回 embedding 为 NULL 的行。"""
        id1 = store.add_fact(FactInput(kind='偏好', content='玩家喜欢写代码'), 0)
        id2 = store.add_fact(FactInput(kind='偏好', content='玩家喜欢听歌'), 0)
        store.store_embedding(id1, _make_vec([1.0, 0.0]))  # 只给第一条

        pending = store.facts_without_embedding()
        ids = [r['id'] for r in pending]
        assert id2 in ids
        assert id1 not in ids

    def test_store_and_retrieve_embedding(self, store):
        """store_embedding 存储，recall_facts 查询时能读到。"""
        fid = store.add_fact(FactInput(kind='偏好', content='玩家不喜欢吃香菜'), 0)
        vec = _make_vec([1.0, 0.0])
        store.store_embedding(fid, vec)

        # 直接验证数据库里有了 embedding
        row = store._db.execute('SELECT embedding FROM facts WHERE id = ?', (fid,)).fetchone()
        assert row[0] is not None
        assert len(row[0]) == 8  # 2 float32 = 8 bytes
