"""SQ8 稠密召回通道验收。

覆盖五件事：

- ``memory/dense.py`` 的批量解码、余弦打分与取前 k；
- 知识检索的词面与稠密两路并集，以及稠密独有条目补算真实 BM25；
- 常驻知识向量索引按 meta 修订号失效，修订号由触发器维护；
- 事实稠密候选沿用词面入口的范围、取代、可见性与纠正过滤；
- 聊天服务把查询向量回调晚绑定给认知动作（构造期绑定会拿到占位服务）。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List
import struct

import numpy as np
import pytest

from src.core.agent.cognition import CognitiveRequest, ConsultAction, RecallAction
from src.core.config.schema import Config
from src.core.memory.dense import (
    KNOWLEDGE_REVISION_KEY,
    KnowledgeVectorIndex,
    cosine_scores,
    q8_cosine,
    q8_matrix,
    query_unit_vector,
    top_k,
)
from src.core.memory.knowledge import add_knowledge, search_knowledge, touch_knowledge
from src.core.memory.quantize import quantize, store_knowledge_vector_pair
from src.core.memory.store import FactInput, MemoryStore
from src.core.memory.tokenize import match_query
from src.core.platform_io.types import DeliveryReceipt, OutboundMessage
from src.core.services.chat import ChatService

_NOW = 1_750_000_000_000
_OWNER = 1


def _vec(*values: float) -> bytes:
    """打包一条小端 float32 向量。"""
    return struct.pack(f'<{len(values)}f', *values)


def _knowledge(db, content: str, vector: bytes | None = None) -> int:
    """写入一条知识并按生产链路补齐原向量与 SQ8。"""
    knowledge_id = add_knowledge(db, content, 'test', _NOW)
    if vector is not None:
        store_knowledge_vector_pair(db, knowledge_id, vector)
    return knowledge_id


def _revision(db) -> int:
    return int(db.execute(
        'SELECT value FROM meta WHERE key = ?', (KNOWLEDGE_REVISION_KEY,),
    ).fetchone()[0])


class TestDenseScoring:
    def test_q8_cosine_tracks_float_cosine(self) -> None:
        """SQ8 余弦与原 float32 余弦的偏差在量化误差范围内。"""
        rng = np.random.default_rng(7)
        rows = rng.standard_normal((40, 64)).astype(np.float32)
        query = rng.standard_normal(64).astype(np.float32)
        matrix = q8_matrix([(i + 1, quantize(row.astype('<f4').tobytes())) for i, row in enumerate(rows)])
        unit = query_unit_vector(query.astype('<f4').tobytes())
        scores = cosine_scores(matrix, unit)
        expected = rows @ query / (np.linalg.norm(rows, axis=1) * np.linalg.norm(query))
        assert np.max(np.abs(scores - expected)) < 0.02
        single = q8_cosine(unit, quantize(rows[3].astype('<f4').tobytes()))
        assert single == pytest.approx(float(scores[3]), abs=1e-5)

    def test_top_k_breaks_ties_by_id(self) -> None:
        """余弦相同的行按主键升序，结果可复现。"""
        same = quantize(_vec(1.0, 0.0))
        matrix = q8_matrix([(9, same), (3, same), (5, same), (4, quantize(_vec(0.0, 1.0)))])
        scores = cosine_scores(matrix, query_unit_vector(_vec(1.0, 0.0)))
        assert [row_id for row_id, _ in top_k(matrix, scores, 3)] == [3, 5, 9]
        assert top_k(matrix, scores, 0) == []

    def test_zero_vector_scores_zero(self) -> None:
        matrix = q8_matrix([(1, quantize(_vec(0.0, 0.0)))])
        assert float(cosine_scores(matrix, query_unit_vector(_vec(1.0, 0.0)))[0]) == 0.0

    def test_matrix_rejects_mixed_dimensions_naming_row(self) -> None:
        with pytest.raises(ValueError, match='第 2 行'):
            q8_matrix([(1, quantize(_vec(1.0, 0.0))), (2, quantize(_vec(1.0, 0.0, 0.0)))])

    def test_matrix_rejects_bad_header_naming_row(self) -> None:
        blob = quantize(_vec(1.0, 0.0))
        with pytest.raises(ValueError, match='第 7 行'):
            q8_matrix([(7, b'XXXX' + blob[4:])])

    def test_query_dimension_mismatch_is_raised(self) -> None:
        """维度不一致说明库被绕过向量空间登记改写过，必须报错而不是跳过。"""
        matrix = q8_matrix([(1, quantize(_vec(1.0, 0.0)))])
        with pytest.raises(ValueError, match='维度'):
            cosine_scores(matrix, query_unit_vector(_vec(1.0, 0.0, 0.0)))


class TestKnowledgeHybridSearch:
    def test_dense_channel_surfaces_entry_without_shared_words(self, db) -> None:
        """与检索词毫无共同字面的条目只能由稠密路召回。"""
        MemoryStore(db)
        semantic = _knowledge(db, '这次更新加入了工具接入', _vec(1.0, 0.0, 0.0))
        lexical = _knowledge(db, '版本号的格式约定', _vec(0.0, 1.0, 0.0))

        lexical_only = search_knowledge(db, '版本号', 5)
        hybrid = search_knowledge(db, '版本号', 5, query_embedding=_vec(1.0, 0.0, 0.0))

        assert [hit.id for hit in lexical_only] == [lexical]
        assert {hit.id for hit in hybrid} == {semantic, lexical}
        assert hybrid[0].id == semantic

    def test_dense_only_entry_gets_real_bm25(self, db) -> None:
        """排不进词面前列、但含检索词的条目经稠密路进入后，词面分按真实 BM25 计。"""
        MemoryStore(db)
        orthogonal = _vec(0.0, 1.0)
        for content in ('咖啡咖啡咖啡', '咖啡 咖啡豆 咖啡', '咖啡 咖啡店', '咖啡 手冲 咖啡'):
            _knowledge(db, content, orthogonal)
        target = _knowledge(
            db, '周末去了很远的地方爬山看风景顺便喝了一杯咖啡然后坐车回家写作业', _vec(1.0, 0.0),
        )
        # 前提：目标条目不在词面路的 limit * 3 = 3 条候选里。
        assert target not in {hit.id for hit in search_knowledge(db, '咖啡', 3)}

        hits = search_knowledge(db, '咖啡', 1, query_embedding=_vec(1.0, 0.0))

        assert hits[0].id == target
        # 余弦为 1 时语义部分恰为 0.6；超出部分来自补算的真实 BM25，记零则恰为 0.6。
        assert hits[0].score > 0.6

    def test_query_without_tokens_still_uses_dense(self, db) -> None:
        MemoryStore(db)
        target = _knowledge(db, '这次更新加入了工具接入', _vec(1.0, 0.0))
        assert match_query('？？') == ''
        assert search_knowledge(db, '？？', 3) == []
        assert [hit.id for hit in search_knowledge(db, '？？', 3, query_embedding=_vec(1.0, 0.0))] == [target]

    def test_corrupt_q8_is_raised_naming_row(self, db) -> None:
        """坏 SQ8 是数据损坏，必须带行号报错，不能静默退回词面。"""
        MemoryStore(db)
        broken = _knowledge(db, '这次更新加入了工具接入', _vec(1.0, 0.0))
        db.execute("UPDATE knowledge SET embedding_q8 = X'00' WHERE id = ?", (broken,))
        db.commit()
        with pytest.raises(ValueError, match=f'第 {broken} 行'):
            search_knowledge(db, '工具', 3, query_embedding=_vec(1.0, 0.0))

    def test_entry_without_vector_keeps_lexical_score(self, db) -> None:
        """尚未补算向量的新条目照常经词面路返回。"""
        MemoryStore(db)
        pending = _knowledge(db, '勾股定理：直角三角形两直角边的平方和等于斜边的平方')
        hits = search_knowledge(db, '勾股定理', 3, query_embedding=_vec(1.0, 0.0))
        assert [hit.id for hit in hits] == [pending]


class TestKnowledgeVectorIndex:
    def test_revision_follows_every_q8_write_path(self, db) -> None:
        """写入、改写、删除 SQ8 都让修订号变化；与向量无关的更新不变。"""
        MemoryStore(db)
        start = _revision(db)
        knowledge_id = _knowledge(db, '甲')
        assert _revision(db) == start, '无向量的插入不影响索引'
        store_knowledge_vector_pair(db, knowledge_id, _vec(1.0, 0.0))
        assert _revision(db) == start + 1
        touch_knowledge(db, [knowledge_id], _NOW)
        assert _revision(db) == start + 1, '命中计数不触及向量'
        store_knowledge_vector_pair(db, knowledge_id, _vec(1.0, 0.0))
        assert _revision(db) == start + 1, '写入相同的 SQ8 不算改动'
        store_knowledge_vector_pair(db, knowledge_id, _vec(0.0, 1.0))
        assert _revision(db) == start + 2
        db.execute('DELETE FROM knowledge WHERE id = ?', (knowledge_id,))
        db.commit()
        assert _revision(db) == start + 3

    def test_index_rebuilds_only_when_revision_changes(self, db) -> None:
        MemoryStore(db)
        index = KnowledgeVectorIndex(db)
        first = _knowledge(db, '甲', _vec(1.0, 0.0))
        built = index.matrix()
        assert built.ids.tolist() == [first]
        assert index.matrix() is built, '修订号未变时复用常驻矩阵'

        second = _knowledge(db, '乙')
        assert index.matrix() is built
        store_knowledge_vector_pair(db, second, _vec(0.0, 1.0))
        assert index.matrix().ids.tolist() == [first, second]

        db.execute('DELETE FROM knowledge WHERE id = ?', (first,))
        db.commit()
        assert index.matrix().ids.tolist() == [second]

    def test_missing_revision_key_is_raised(self, db) -> None:
        MemoryStore(db)
        db.execute('DELETE FROM meta WHERE key = ?', (KNOWLEDGE_REVISION_KEY,))
        db.commit()
        with pytest.raises(RuntimeError, match=KNOWLEDGE_REVISION_KEY):
            KnowledgeVectorIndex(db).matrix()


class TestFactDenseCandidates:
    def test_store_embedding_writes_q8_in_same_update(self, db) -> None:
        store = MemoryStore(db)
        fact_id = store.add_fact(_OWNER, FactInput(kind='偏好', content='他喜欢手冲咖啡'), _NOW).fact_id
        vector = _vec(0.6, -0.8)
        store.store_embedding(fact_id, vector)
        row = db.execute('SELECT embedding, embedding_q8 FROM facts WHERE id = ?', (fact_id,)).fetchone()
        assert row[0] == vector
        assert row[1] == quantize(vector)

    def test_candidates_follow_reader_filters(self, db) -> None:
        """人物范围、已取代、场合可见性、已被纠正、已在池中五道过滤都生效。"""
        store = MemoryStore(db)
        other = db.execute(
            "INSERT INTO persons (kind, first_seen_at) VALUES ('contact', ?)", (_NOW,),
        ).lastrowid
        db.commit()
        aligned = _vec(1.0, 0.0)

        def fact(person_id: int, content: str, origin: str = 'group') -> int:
            fact_id = store.add_fact(
                person_id, FactInput(kind='偏好', content=content, origin_kind=origin), _NOW,
            ).fact_id
            store.store_embedding(fact_id, aligned)
            return fact_id

        visible = fact(_OWNER, '他每天晨跑五公里')
        in_pool = fact(_OWNER, '他养了一只橘猫')
        private = fact(_OWNER, '他最近在看心理医生', origin='direct')
        superseded = fact(_OWNER, '他住在杭州')
        replacement = fact(_OWNER, '他搬到了上海')
        marked = fact(_OWNER, '他讨厌香菜')
        outsider = fact(other, '她喜欢爬山')
        db.execute('UPDATE facts SET superseded_by = ? WHERE id = ?', (replacement, superseded))
        db.execute(
            '''INSERT INTO memory_feedback_results
               (fact_id, person_id, stream_id, confidence, marked, created_at)
               VALUES (?, ?, 1, 0.9, 1, ?)''',
            (marked, _OWNER, _NOW),
        )
        db.commit()

        found = store.dense_fact_candidates(
            (_OWNER,), '随便聊聊', aligned, 5, _NOW,
            stream_kind='group', exclude_marked=True, exclude_ids={in_pool},
        )

        assert {f.id for f in found} == {visible, replacement}
        assert private not in {f.id for f in found}
        assert outsider not in {f.id for f in found}
        assert all(f.lexical_relevance == 0.0 for f in found), '检索词与正文无共同字面'
        across = store.dense_fact_candidates(
            None, '随便聊聊', aligned, 5, _NOW, stream_kind='group',
        )
        assert outsider in {f.id for f in across}

    async def test_recall_action_merges_dense_candidates(self, db) -> None:
        """recall 装配了查询向量时能想起与检索词无共同字面的事实。"""
        store = MemoryStore(db)
        fact_id = store.add_fact(
            _OWNER, FactInput(kind='偏好', content='他每天晨跑五公里', origin_kind='direct'), _NOW,
        ).fact_id
        store.store_embedding(fact_id, _vec(1.0, 0.0))
        request = CognitiveRequest(
            action='recall', query='运动习惯', stream_id=1, stream_kind='direct',
            person_ids=(_OWNER,), message_watermark=0,
        )

        async def embed_query(text: str) -> bytes:
            return _vec(1.0, 0.0)

        lexical_only = await RecallAction(store, lambda person, stream: '他', db).execute(request)
        hybrid = await RecallAction(
            store, lambda person, stream: '他', db, embed_query=embed_query,
        ).execute(request)

        assert lexical_only.hit_count == 0
        assert '晨跑' in hybrid.text


async def test_consult_searches_off_event_loop(db, monkeypatch) -> None:
    """知识检索（含索引重建）在线程池里执行，不占事件循环线程。"""
    import threading

    from src.core.agent import cognition

    MemoryStore(db)
    target = _knowledge(db, '这次更新加入了工具接入', _vec(1.0, 0.0))
    on_loop_thread: List[bool] = []
    real_search = cognition.search_knowledge

    def spy(*args: Any, **kwargs: Any):
        on_loop_thread.append(threading.current_thread() is threading.main_thread())
        return real_search(*args, **kwargs)

    monkeypatch.setattr(cognition, 'search_knowledge', spy)

    async def embed_query(text: str) -> bytes:
        return _vec(1.0, 0.0)

    result = await ConsultAction(db, embed_query=embed_query).execute(CognitiveRequest(
        action='consult', query='工具', stream_id=1, stream_kind='direct',
        person_ids=(_OWNER,), message_watermark=0,
    ))

    assert on_loop_thread == [False]
    assert result.hit_count == 1
    row = db.execute('SELECT hit_count FROM knowledge WHERE id = ?', (target,)).fetchone()
    assert row[0] == 1, '命中计数在事件循环线程上照常落库'


class _Provider:
    provider = 'fake'
    model = 'fake-model'

    async def stream(self, **kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        yield {'text': ''}


class _Broker:
    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        return DeliveryReceipt(platform=message.stream.platform, stream_id=message.stream.id, external_message_ids=[])


class _Vector:
    """模拟 __main__ 在聊天服务构造完成后才装配的真实向量服务。"""

    def __init__(self) -> None:
        self.queries: List[str] = []

    async def embed_query(self, text: str) -> bytes:
        self.queries.append(text)
        return _vec(1.0, 0.0)


async def test_cognitive_actions_resolve_vector_service_late(db, monkeypatch, tmp_path) -> None:
    """构造后替换的向量服务必须被 consult 与 recall 用上。

    回归：consult 曾在构造期绑定占位服务的 embed_query，生产上恒为纯词面且无报错。
    """
    from src.core.services.chat import service as service_module

    monkeypatch.setattr(service_module, 'PLUGIN_ROOTS', ((tmp_path, False),))
    config = Config()
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.max_cognitive_rounds = 2

    async def push(_channel: str, _payload: Any, _stream_id: int) -> None:
        pass

    provider = _Provider()
    chat = ChatService(db, provider, None, None, push, cfg=config, data_dir=tmp_path, broker=_Broker(),
                       planner_provider=provider, replyer_provider=provider)
    vector = _Vector()
    chat._vector = vector

    for name in ('consult', 'recall'):
        action = chat._tool_registry.resolve(name).executor._action
        assert await action._embed_query(f'{name} 检索词') == _vec(1.0, 0.0)
    assert vector.queries == ['consult 检索词', 'recall 检索词']
    assert isinstance(chat._tool_registry.resolve('consult').executor._action, ConsultAction)
