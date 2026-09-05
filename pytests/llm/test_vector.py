"""
向量召回测试。

不调真实 embedding API：用假向量验证 BM25+向量混合打分的逻辑，
以及无向量时纯 BM25 正常降级。
"""

from __future__ import annotations

import struct
import sqlite3
import pytest

from src.core.memory.store import EpisodeInput, FactInput, MemoryStore
from src.core.memory.embed import cosine, _pack, _unpack
from src.core.memory.knowledge import add_knowledge
from src.core.memory.quantize import dequantize
from src.core.memory.store import RecalledFact
from src.core.services.maintenance.vector import VectorService

OWNER_PERSON_ID = 1


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
        id1 = store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='玩家不喜欢吃香菜'), 0)
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='玩家养了一只猫'), 0)
        results = store.recall_facts(OWNER_PERSON_ID, '香菜', 5, stream_kind='direct')
        assert len(results) > 0
        assert results[0].content == '玩家不喜欢吃香菜'

    def test_bm25_fallback_when_no_query_embedding(self, store):
        """query_embedding=None 时不使用向量，只用 BM25。"""
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='玩家喜欢 Rust 编程'), 0)
        results = store.recall_facts(
            OWNER_PERSON_ID, 'Rust', 5, query_embedding=None, stream_kind='direct',
        )
        assert any('Rust' in r.content for r in results)

    def test_hybrid_score_fuses_bm25_and_vector(self, store):
        """有向量时，高余弦相似度事实的排名应高于低余弦的事实（BM25 匹配度相同时）。

        查询词选择「玩家喜欢」，使两条事实都进入候选集且 BM25 分值接近，
        向量相似度差异才能独立影响排名；若只命中一条，测试将退化为候选集过滤验证。
        """
        id_coding = store.add_fact(OWNER_PERSON_ID, FactInput(kind='习惯', content='玩家喜欢写代码做项目'), 0).fact_id
        id_music = store.add_fact(OWNER_PERSON_ID, FactInput(kind='习惯', content='玩家喜欢听音乐放松'), 0).fact_id

        # 归一化假向量：coding=[1,0], music=[0,1], query≈coding
        vec_coding = _make_vec([1.0, 0.0])
        vec_music = _make_vec([0.0, 1.0])
        query_vec = _make_vec([0.95, 0.05])

        store.store_embedding(id_coding, vec_coding)
        store.store_embedding(id_music, vec_music)

        results = store.recall_facts(
            OWNER_PERSON_ID, '玩家喜欢', 5, query_embedding=query_vec, stream_kind='direct',
        )
        ids = [r.id for r in results]
        assert id_coding in ids and id_music in ids, f'两条事实都应出现在召回结果里: {ids}'
        assert ids.index(id_coding) < ids.index(id_music), (
            f'向量更接近 coding 时，coding({id_coding}) 应排在 music({id_music}) 前面，实际: {ids}'
        )

    def test_partial_embedding_graceful(self, store):
        """部分事实有向量、部分没有，不应崩溃。"""
        id1 = store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='玩家喜欢猫'), 0).fact_id
        id2 = store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='玩家喜欢狗'), 0).fact_id
        # 只给第一条存向量
        store.store_embedding(id1, _make_vec([1.0, 0.0]))
        query_vec = _make_vec([1.0, 0.0])

        results = store.recall_facts(
            OWNER_PERSON_ID, '宠物 猫', 5, query_embedding=query_vec, stream_kind='direct',
        )
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
        id1 = store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='玩家喜欢写代码'), 0).fact_id
        id2 = store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='玩家喜欢听歌'), 0).fact_id
        store.store_embedding(id1, _make_vec([1.0, 0.0]))  # 只给第一条

        pending = store.facts_without_embedding()
        ids = [r['id'] for r in pending]
        assert id2 in ids
        assert id1 not in ids

    def test_store_and_retrieve_embedding(self, store):
        """store_embedding 存储，recall_facts 查询时能读到。"""
        fid = store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='玩家不喜欢吃香菜'), 0).fact_id
        vec = _make_vec([1.0, 0.0])
        store.store_embedding(fid, vec)

        # 直接验证数据库里有了 embedding
        row = store._db.execute('SELECT embedding FROM facts WHERE id = ?', (fid,)).fetchone()
        assert row[0] is not None
        assert len(row[0]) == 8  # 2 float32 = 8 bytes

    def test_rank_recalled_facts_executes_semantic_fusion(self, store):
        """★V-3：词面分相同的候选必须由向量差异改变排序。"""

        query = _make_vec([1.0, 0.0])
        lexical_first = RecalledFact(
            id=1,
            kind='偏好',
            content='词面候选一',
            retention=1.0,
            score=1.0,
            lexical_relevance=0.5,
            embedding=_make_vec([0.0, 1.0]),
        )
        semantic_first = RecalledFact(
            id=2,
            kind='偏好',
            content='词面候选二',
            retention=1.0,
            score=1.0,
            lexical_relevance=0.5,
            embedding=_make_vec([1.0, 0.0]),
        )

        lexical = store.rank_recalled_facts([lexical_first, semantic_first], None, 2)
        hybrid = store.rank_recalled_facts([lexical_first, semantic_first], query, 2)

        assert [fact.id for fact in lexical] == [1, 2]
        assert [fact.id for fact in hybrid] == [2, 1]


@pytest.mark.asyncio
class TestVectorServiceLifecycle:
    async def test_embed_fact_writes_raw_and_q8_in_same_chain(self, store):
        """新事实拿到原向量后，同一调用链同步派生 SQ8。"""

        vector = _make_vec([1.0, -0.5, 0.25])

        class _Client:
            async def embed_one(self, text):
                assert text == '需要同步量化的事实'
                return vector

        fact_id = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(kind='事件', content='需要同步量化的事实'),
            0,
        )
        service = VectorService(store, _Client(), db=store._db)

        await service.embed_fact(fact_id.fact_id, '需要同步量化的事实')

        row = store._db.execute(
            'SELECT embedding, embedding_q8 FROM facts WHERE id = ?',
            (fact_id.fact_id,),
        ).fetchone()
        assert row['embedding'] == vector
        assert row['embedding_q8'] is not None
        assert len(dequantize(row['embedding_q8'])) == len(vector)

    async def test_embed_knowledge_writes_raw_and_q8_in_same_chain(self, store):
        """★K-1：一条在线知识在返回前同时拿到原向量与 SQ8。"""

        vector = _make_vec([1.0, -0.5, 0.25])

        class _Client:
            async def embed_one(self, text):
                assert text == '月球没有全球性磁场'
                return vector

        knowledge_id = add_knowledge(
            store._db,
            '月球没有全球性磁场',
            'fact_extract',
            1,
        )
        service = VectorService(store, _Client(), db=store._db)

        await service.embed_knowledge(knowledge_id, '月球没有全球性磁场')

        row = store._db.execute(
            'SELECT embedding, embedding_q8 FROM knowledge WHERE id = ?',
            (knowledge_id,),
        ).fetchone()
        assert row['embedding'] == vector
        assert row['embedding_q8'] is not None
        assert len(dequantize(row['embedding_q8'])) == len(vector)

    async def test_startup_backfills_missing_knowledge_raw_and_q8(self, store):
        """★K-2/K-3：启动补算清零知识缺口并保持两列逐行同覆盖。"""

        vector = _make_vec([0.25, 1.0, -0.5])

        class _Client:
            async def embed(self, texts):
                return [vector for _ in texts]

        for index in range(3):
            add_knowledge(
                store._db,
                f'待补算知识 {index}',
                'fact_extract',
                index + 1,
            )
        service = VectorService(store, _Client(), db=store._db)

        await service.startup()
        task = service._backfill_task
        assert task is not None
        await task

        assert store._db.execute(
            'SELECT COUNT(*) FROM knowledge WHERE embedding IS NULL'
        ).fetchone()[0] == 0
        assert store._db.execute(
            '''SELECT COUNT(*) FROM knowledge
               WHERE (embedding IS NULL) <> (embedding_q8 IS NULL)'''
        ).fetchone()[0] == 0
        await service.shutdown()

    async def test_knowledge_backfill_attempts_each_pending_row_once(self, store):
        """provider 返回空时本次不死循环，失败行留给下次启动。"""

        class _Client:
            def __init__(self):
                self.calls = 0

            async def embed(self, texts):
                self.calls += 1
                return [None for _ in texts]

        for index in range(3):
            add_knowledge(
                store._db,
                f'补算失败知识 {index}',
                'fact_extract',
                index + 1,
            )
        client = _Client()
        service = VectorService(store, client, db=store._db)

        assert await service.backfill_knowledge() == 0
        assert client.calls == 1
        assert len(store._db.execute(
            'SELECT id FROM knowledge WHERE embedding IS NULL'
        ).fetchall()) == 3

    async def test_missing_knowledge_is_reported_at_startup(self, store, monkeypatch):
        """装配成功但知识缺向量时，启动期必须显式给出待补算数量。"""

        vector = _make_vec([1.0, 0.5])

        class _Client:
            async def embed(self, texts):
                return [vector for _ in texts]

        class _Logger:
            def warning(self, event, **fields):
                warnings.append((event, fields))

            def info(self, event, **fields):
                pass

        warnings = []
        monkeypatch.setattr('src.core.services.maintenance.vector.logger', _Logger())
        add_knowledge(store._db, '启动期缺失向量的知识', 'fact_extract', 1)
        service = VectorService(store, _Client(), db=store._db)

        await service.startup()
        task = service._backfill_task
        assert task is not None
        await task

        assert ('vector_knowledge_backfill_pending', {'count': 1}) in warnings
        await service.shutdown()

    async def test_online_knowledge_embedding_failure_is_visible(self, store, monkeypatch):
        """在线 provider 返回空值时保留双 NULL，并在同一向量服务日志中告警。"""

        class _Client:
            async def embed_one(self, text):
                return None

        class _Logger:
            def warning(self, event, **fields):
                warnings.append((event, fields))

        warnings = []
        monkeypatch.setattr('src.core.services.maintenance.vector.logger', _Logger())
        knowledge_id = add_knowledge(store._db, '无法生成向量的知识', 'fact_extract', 1)
        service = VectorService(store, _Client(), db=store._db)

        await service.embed_knowledge(knowledge_id, '无法生成向量的知识')

        row = store._db.execute(
            'SELECT embedding, embedding_q8 FROM knowledge WHERE id = ?',
            (knowledge_id,),
        ).fetchone()
        assert tuple(row) == (None, None)
        assert warnings == [(
            'vector_knowledge_write_failed',
            {'id': knowledge_id, 'reason': 'embedding 返回空值'},
        )]

    async def test_startup_backfills_existing_q8_rows(self, store):
        """启动钩子立即返回后台任务，任务会补齐两张表的已有原向量。"""

        vector = _make_vec([0.25, 1.0, -0.5])

        class _Client:
            async def embed(self, texts):
                return [vector for _ in texts]

        store._db.execute(
            '''INSERT INTO knowledge (
                 content, content_key, source, tokens_v2, embedding, created_at
               ) VALUES ('启动补算知识', 'startup-q8', 'test', '', ?, 1)''',
            (vector,),
        )
        store._db.commit()
        service = VectorService(store, _Client(), db=store._db)

        await service.startup()
        task = service._backfill_task
        assert task is not None
        await task

        assert store._db.execute(
            "SELECT embedding_q8 IS NOT NULL FROM knowledge WHERE content_key = 'startup-q8'"
        ).fetchone()[0] == 1
        await service.shutdown()

    async def test_disabled_service_warns_once(self, monkeypatch):
        """★V-4：未装配不能与正常但无待办混成同一种静默状态。"""

        class _Logger:
            def warning(self, event, **fields):
                warnings.append((event, fields))

        warnings = []
        monkeypatch.setattr(
            'src.core.services.maintenance.vector.logger',
            _Logger(),
        )
        service = VectorService(None, None, disabled_reason='vector.enabled=false')

        await service.startup()

        assert warnings == [(
            'vector_service_disabled',
            {'reason': 'vector.enabled=false'},
        )]

    async def test_backfill_attempts_each_pending_fact_once(self, store):
        """失败项保留 NULL 给下次启动，不在同一次补算里形成无限循环。"""

        class _Client:
            def __init__(self):
                self.calls = 0

            async def embed(self, texts):
                self.calls += 1
                return [None for _ in texts]

        for index in range(3):
            store.add_fact(
                OWNER_PERSON_ID,
                FactInput(kind='事件', content=f'待补算事实 {index}'),
                0,
            )
        client = _Client()
        service = VectorService(store, client)

        assert await service.backfill() == 0
        assert client.calls == 1
        assert len(store.facts_without_embedding()) == 3
