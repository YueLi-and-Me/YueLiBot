"""检索评估第二迭代的机检断言。

覆盖四条验收断言：
★E-1 重放使用留痕检索词，候选集合与 candidatePool 一致（含对不上时的
   单独计数与 missing/extra 归因）；
★E-2 无留痕的回合不进入新口径统计（样本池分离）；
★E-3 融合打分与生产同口径（评估重排结果与生产 rank_recalled_facts 直调
   逐条一致，默认参数下 blend_score 与 decay.score 逐字节等价）；
★E-4 报告按 stream_kind 分组，整体 / 群聊 / 私聊三列各自独立。
另覆盖查询向量缓存计数、向量缺席时的退化与评估的只读性。
"""

from __future__ import annotations

import json
import struct

import pytest

from src.core.memory import tuning
from src.core.memory.decay import relevance_from_bm25, score as legacy_score
from src.core.memory.store import FactInput, MemoryStore

OWNER_PERSON_ID = 1
FRIEND_PERSON_ID = 2
NOW = 1_800_000_000_000


def _pack(*values: float) -> bytes:
    """按事实向量的存储格式打包一组 float32。"""

    return struct.pack(f'<{len(values)}f', *values)


def _seed_stream(db, stream_id: int, kind: str) -> None:
    db.execute(
        "INSERT INTO streams (id, platform, kind, external_id) VALUES (?, 'qq', ?, ?)",
        (stream_id, kind, f'ext-{stream_id}'),
    )
    db.commit()


def _seed_message(
    db, stream_id: int, sender: int | None, content: str, created_at: int,
) -> None:
    role = 'assistant' if sender is None else 'user'
    db.execute(
        'INSERT INTO messages (role, content, created_at, stream_id, sender_person_id)'
        ' VALUES (?, ?, ?, ?, ?)',
        (role, content, created_at, stream_id, sender),
    )
    db.commit()


def _insert_event(
    db, kind: str, payload: dict, *, at: int, stream_id: int, turn_id: int | None,
) -> None:
    db.execute(
        'INSERT INTO pipeline_events (at, stream_id, turn_id, stage, kind, payload)'
        " VALUES (?, ?, ?, '', ?, ?)",
        (at, stream_id, turn_id, kind, json.dumps(payload, ensure_ascii=False)),
    )
    db.commit()


def _insert_retrieval_trace(
    db,
    *,
    turn_id: int,
    stream_id: int,
    at: int,
    current_text: str,
    impression: str,
    pool: list[tuple[int, float]],
    prompt_fact_ids: list[int],
    replied: bool = True,
) -> None:
    _insert_event(
        db,
        'memory_retrieval_trace',
        {
            'currentText': current_text,
            'conversationImpression': impression,
            'currentTextChars': len(current_text),
            'impressionChars': len(impression),
            'candidateCount': len(pool),
            'candidatePool': [
                {'factId': fact_id, 'score': score} for fact_id, score in pool
            ],
            'promptFactIds': prompt_fact_ids,
        },
        at=at, stream_id=stream_id, turn_id=turn_id,
    )
    if replied:
        _insert_event(
            db, 'outbound_delivered', {'text': 'ok'},
            at=at + 1, stream_id=stream_id, turn_id=turn_id,
        )


def _make_store(db) -> MemoryStore:
    # owner（person 1）由 schema 种子插入，这里补齐第二位在场者。
    db.execute("INSERT OR IGNORE INTO persons (id, kind, first_seen_at)"
               " VALUES (?, 'owner', ?)", (OWNER_PERSON_ID, NOW))
    db.execute("INSERT OR IGNORE INTO persons (id, kind, first_seen_at)"
               " VALUES (?, 'friend', ?)", (FRIEND_PERSON_ID, NOW))
    db.commit()
    return MemoryStore(db)


async def _fixed_vector(vector: bytes):
    async def embed(text: str) -> bytes | None:
        return vector
    return embed


class TestTurnDedup:
    """无留痕样本构建的回合去重：planner 同回合多次调用只计一次。"""

    def test_duplicate_prompt_records_collapse_to_one_turn(self, db, tmp_path):
        store = _make_store(db)
        _seed_stream(db, 102, 'direct')
        _seed_message(db, 102, OWNER_PERSON_ID, '聊聊咖啡', NOW - 1_000)
        coffee = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他只喝手冲咖啡', kind='偏好', origin_kind='direct'),
            NOW,
        ).fact_id
        _insert_event(
            db, 'outbound_delivered', {'text': 'ok'},
            at=NOW, stream_id=102, turn_id=40,
        )
        # 同一回合两份合格转储：planner 纠错重试的形态。
        for index in (1, 2):
            dump = {
                'request': {'messages': [
                    {'role': 'system', 'content': f'[长期记忆]\n- 某人 他只喝手冲咖啡\n（第 {index} 份）'},
                    {'role': 'user', 'content': '咖啡 手冲'},
                ]}
            }
            path = tmp_path / f'planner-turn40-{index}.json'
            path.write_text(json.dumps(dump, ensure_ascii=False), encoding='utf-8')
            _insert_event(
                db, 'prompt_record', {'task': 'planner', 'path': str(path)},
                at=NOW, stream_id=102, turn_id=40,
            )
        samples = tuning.build_turn_samples(db)
        assert [sample.turn_id for sample in samples] == [40]
        assert samples[0].positive_fact_ids == [coffee]


class TestReconcilePool:
    """留痕对账纯函数件。"""

    def test_identical_pools_match(self):
        pool = [(11, 0.5), (12, 0.4)]

        class _F:
            def __init__(self, fact_id, score):
                self.id, self.score = fact_id, score

        replay = [_F(11, 0.5), _F(12, 0.4)]
        result = tuning.reconcile_trace_pool(replay, pool)
        assert result.status == 'match'
        assert result.max_score_delta == 0.0

    def test_missing_and_extra_reported_separately(self):
        class _F:
            def __init__(self, fact_id, score):
                self.id, self.score = fact_id, score

        result = tuning.reconcile_trace_pool(
            [_F(11, 0.5), _F(13, 0.3)], [(11, 0.5), (12, 0.4)]
        )
        assert result.status == 'mismatch'
        assert result.missing == [12]
        assert result.extra == [13]
        assert result.order_changed is False


class TestE1ReplayUsesTraceQueries:
    """★E-1：重放读留痕检索词，候选集合与 candidatePool 一致。"""

    def _prepare(self, db):
        store = _make_store(db)
        _seed_stream(db, 101, 'group')
        # 在场者由消息时间线决定：说话人 1 在最后，2 在其前开口。
        _seed_message(db, 101, FRIEND_PERSON_ID, '聊点别的', NOW - 3_000)
        _seed_message(db, 101, OWNER_PERSON_ID, '聊聊咖啡', NOW - 1_000)
        coffee = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他只喝手冲咖啡', kind='偏好', origin_kind='group'),
            NOW,
        ).fact_id
        jargon = store.add_fact(
            FRIEND_PERSON_ID,
            FactInput(content='她在学爵士钢琴', kind='习惯', origin_kind='group'),
            NOW,
        ).fact_id
        return store, coffee, jargon

    async def test_replay_matches_trace_pool(self, db):
        store, coffee, jargon = self._prepare(db)
        present = tuning.approximate_present_persons(store, 101, NOW)
        assert present[0] == OWNER_PERSON_ID
        assert FRIEND_PERSON_ID in present
        replay = tuning.replay_union_recall(
            store, present, '咖啡 手冲', '爵士 咖啡', 6, NOW,
            stream_kind='group',
        )
        pool = [(fact.id, fact.score) for fact in replay]
        assert {coffee, jargon} <= {fact_id for fact_id, _ in pool}
        _insert_retrieval_trace(
            db,
            turn_id=7, stream_id=101, at=NOW,
            current_text='咖啡 手冲', impression='爵士 咖啡',
            pool=pool, prompt_fact_ids=[replay[0].id],
        )
        samples = tuning.build_trace_samples(db)
        assert len(samples) == 1
        assert samples[0].scorable is True
        report = await tuning.evaluate_round2(store, samples, [], {})
        assert report.traced['sample_count'] == 1
        assert report.traced['reconcile_matched'] == 1
        assert report.traced['reconcile_mismatched'] == 0
        assert report.per_turn[0]['reconcile']['status'] == 'match'
        # 向量缺席时融合退化为词面排序，而重放池本身就是词面序。
        assert report.per_turn[0]['ranked_ids'] == [
            fact.id for fact in replay
        ][: report.k]

    async def test_mismatched_turn_counted_separately(self, db):
        """对不上的回合单独计数：缺失与顺序差异都算 mismatch。"""

        store, coffee, jargon = self._prepare(db)
        present = tuning.approximate_present_persons(store, 101, NOW)
        replay = tuning.replay_union_recall(
            store, present, '咖啡 手冲', '爵士 咖啡', 6, NOW,
            stream_kind='group',
        )
        pool = [(fact.id, fact.score) for fact in replay]
        # 留痕里多记一条重放找不到的事实：事实被取代或冻结时的形态。
        tampered = [*pool, (99_999, 0.01)]
        _insert_retrieval_trace(
            db, turn_id=8, stream_id=101, at=NOW,
            current_text='咖啡 手冲', impression='爵士 咖啡',
            pool=tampered, prompt_fact_ids=[replay[0].id],
        )
        report = await tuning.evaluate_round2(
            store, tuning.build_trace_samples(db), [], {}
        )
        assert report.traced['reconcile_mismatched'] == 1
        assert report.per_turn[0]['reconcile']['missing'] == [99_999]

    async def test_empty_prompt_facts_still_reconciled(self, db):
        """无正例的留痕回合不进 nDCG，但候选池仍要对账。"""

        store, coffee, jargon = self._prepare(db)
        present = tuning.approximate_present_persons(store, 101, NOW)
        replay = tuning.replay_union_recall(
            store, present, '咖啡 手冲', '爵士 咖啡', 6, NOW,
            stream_kind='group',
        )
        pool = [(fact.id, fact.score) for fact in replay]
        _insert_retrieval_trace(
            db, turn_id=9, stream_id=101, at=NOW,
            current_text='咖啡 手冲', impression='爵士 咖啡',
            pool=pool, prompt_fact_ids=[], replied=False,
        )
        report = await tuning.evaluate_round2(
            store, tuning.build_trace_samples(db), [], {}
        )
        assert report.traced['sample_count'] == 1
        assert report.per_turn[0]['scorable'] is False
        assert report.per_turn[0]['reconcile']['status'] == 'match'


class TestE2PoolSeparation:
    """★E-2：无留痕的回合不进入新口径统计。"""

    async def test_turn_without_trace_never_enters_traced_pool(self, db):
        store = _make_store(db)
        _seed_stream(db, 101, 'direct')
        _seed_message(db, 101, OWNER_PERSON_ID, '聊聊咖啡', NOW - 1_000)
        coffee = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他只喝手冲咖啡', kind='偏好', origin_kind='direct'),
            NOW,
        ).fact_id
        # 只有回复事件，没有任何 memory_retrieval_trace。
        _insert_event(
            db, 'outbound_delivered', {'text': 'ok'},
            at=NOW, stream_id=101, turn_id=3,
        )
        assert tuning.build_trace_samples(db) == []
        untraced = [
            tuning.TurnSample(
                turn_id=3, stream_id=101, stream_kind='direct', at=NOW,
                query='咖啡 手冲',
                present_person_ids=[OWNER_PERSON_ID],
                positive_fact_ids=[coffee],
            )
        ]
        report = await tuning.evaluate_round2(store, [], untraced, {})
        assert report.traced['sample_count'] == 0
        assert report.untraced['sample_count'] == 1
        assert report.per_turn[0]['pool'] == 'untraced'
        assert report.per_turn[0]['reconcile'] is None

    async def test_traced_and_untraced_never_merge(self, db):
        """两池同跑时 per_turn 各自标记，汇总各自计数。"""

        store = _make_store(db)
        _seed_stream(db, 101, 'direct')
        _seed_message(db, 101, OWNER_PERSON_ID, '聊聊咖啡', NOW - 1_000)
        coffee = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他只喝手冲咖啡', kind='偏好', origin_kind='direct'),
            NOW,
        ).fact_id
        present = tuning.approximate_present_persons(store, 101, NOW)
        replay = tuning.replay_union_recall(
            store, present, '咖啡 手冲', '', 6, NOW, stream_kind='direct',
        )
        _insert_retrieval_trace(
            db, turn_id=1, stream_id=101, at=NOW,
            current_text='咖啡 手冲', impression='',
            pool=[(fact.id, fact.score) for fact in replay],
            prompt_fact_ids=[coffee],
        )
        untraced = [
            tuning.TurnSample(
                turn_id=2, stream_id=101, stream_kind='direct', at=NOW,
                query='咖啡 手冲',
                present_person_ids=[OWNER_PERSON_ID],
                positive_fact_ids=[coffee],
            )
        ]
        report = await tuning.evaluate_round2(
            store, tuning.build_trace_samples(db), untraced, {}
        )
        assert report.traced['sample_count'] == 1
        assert report.untraced['sample_count'] == 1
        pools = {row['pool'] for row in report.per_turn}
        assert pools == {'traced', 'untraced'}


class TestE3FusionMatchesProduction:
    """★E-3：融合打分与生产同口径。"""

    async def test_round2_ranking_equals_production_ranker(self, db):
        store = _make_store(db)
        _seed_stream(db, 101, 'direct')
        _seed_message(db, 101, OWNER_PERSON_ID, '聊聊咖啡', NOW - 1_000)
        near = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他只喝手冲咖啡', kind='偏好', origin_kind='direct'),
            NOW,
        ).fact_id
        far = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他去咖啡馆自习', kind='习惯', origin_kind='direct'),
            NOW,
        ).fact_id
        # 与查询同向的事实在语义侧得满分，正交事实居中：融合序由生产公式决定。
        store.store_embedding(near, _pack(1.0, 0.0, 0.0, 0.0))
        store.store_embedding(far, _pack(0.0, 1.0, 0.0, 0.0))
        query_vector = _pack(1.0, 0.0, 0.0, 0.0)
        present = tuning.approximate_present_persons(store, 101, NOW)
        replay = tuning.replay_union_recall(
            store, present, '咖啡 手冲', '', 6, NOW, stream_kind='direct',
        )
        production = store.rank_recalled_facts(replay, query_vector, 6)
        _insert_retrieval_trace(
            db, turn_id=5, stream_id=101, at=NOW,
            current_text='咖啡 手冲', impression='',
            pool=[(fact.id, fact.score) for fact in replay],
            prompt_fact_ids=[near, far],
        )
        report = await tuning.evaluate_round2(
            store, tuning.build_trace_samples(db), [], {},
            embed_query=await _fixed_vector(query_vector),
        )
        assert report.per_turn[0]['ranked_ids'] == [fact.id for fact in production]
        assert report.per_turn[0]['ndcg'] == 1.0

    def test_blend_score_equals_legacy_formula_bitwise(self):
        """默认参数下 blend_score 与生产 decay.score 逐字节等价。"""

        tuning.set_active_overrides({})
        try:
            for bm25, retention_value in [(-1.2, 0.0), (-3.4, 0.42), (-0.1, 1.0)]:
                relevance = relevance_from_bm25(bm25)
                assert tuning.blend_score(
                    relevance, retention_value
                ) == legacy_score(bm25, retention_value)
        finally:
            tuning.set_active_overrides({})

    async def test_vector_cache_counts_calls(self, db):
        """同一检索词只调用一次查询向量，命中走缓存并计数。"""

        store = _make_store(db)
        _seed_stream(db, 101, 'direct')
        _seed_message(db, 101, OWNER_PERSON_ID, '聊聊咖啡', NOW - 1_000)
        coffee = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他只喝手冲咖啡', kind='偏好', origin_kind='direct'),
            NOW,
        ).fact_id
        present = tuning.approximate_present_persons(store, 101, NOW)
        replay = tuning.replay_union_recall(
            store, present, '咖啡 手冲', '', 6, NOW, stream_kind='direct',
        )
        for turn in (11, 12):
            _insert_retrieval_trace(
                db, turn_id=turn, stream_id=101, at=NOW,
                current_text='咖啡 手冲', impression='',
                pool=[(fact.id, fact.score) for fact in replay],
                prompt_fact_ids=[coffee],
            )
        calls = 0

        async def counting_embed(text: str) -> bytes | None:
            nonlocal calls
            calls += 1
            return _pack(1.0, 0.0, 0.0, 0.0)

        report = await tuning.evaluate_round2(
            store, tuning.build_trace_samples(db), [], {}, embed_query=counting_embed,
        )
        assert calls == 1
        assert report.embedding['calls'] == 1
        assert report.embedding['cache_hits'] == 1

    async def test_missing_vector_service_degrades(self, db):
        """向量缺席时融合退化为词面排序，不报错、零调用。"""

        store = _make_store(db)
        _seed_stream(db, 101, 'direct')
        _seed_message(db, 101, OWNER_PERSON_ID, '聊聊咖啡', NOW - 1_000)
        coffee = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他只喝手冲咖啡', kind='偏好', origin_kind='direct'),
            NOW,
        ).fact_id
        present = tuning.approximate_present_persons(store, 101, NOW)
        replay = tuning.replay_union_recall(
            store, present, '咖啡 手冲', '', 6, NOW, stream_kind='direct',
        )
        _insert_retrieval_trace(
            db, turn_id=13, stream_id=101, at=NOW,
            current_text='咖啡 手冲', impression='',
            pool=[(fact.id, fact.score) for fact in replay],
            prompt_fact_ids=[coffee],
        )
        report = await tuning.evaluate_round2(
            store, tuning.build_trace_samples(db), [], {},
        )
        assert report.embedding['calls'] == 0
        assert report.per_turn[0]['ndcg'] > 0.0


class TestE4StreamGroups:
    """★E-4：报告按 stream_kind 分组，三列各自独立。"""

    async def test_three_columns_independent(self, db):
        store = _make_store(db)
        _seed_stream(db, 101, 'group')
        _seed_stream(db, 102, 'direct')
        _seed_message(db, 101, OWNER_PERSON_ID, '群聊咖啡', NOW - 1_000)
        _seed_message(db, 101, FRIEND_PERSON_ID, '群聊闲话', NOW - 2_000)
        _seed_message(db, 102, OWNER_PERSON_ID, '私聊咖啡', NOW - 1_000)
        inside = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他只喝手冲咖啡', kind='偏好', origin_kind='group'),
            NOW,
        ).fact_id
        # 与检索词无关的事实永远进不了池：把它设为正例即得 nDCG=0、被挤掉 1。
        outside = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他在学爵士钢琴', kind='习惯', origin_kind='group'),
            NOW,
        ).fact_id
        plan = [
            # (stream, kind, 正例, 期望 nDCG)
            (101, 'group', [inside], 1.0),
            (101, 'group', [outside], 0.0),
            (102, 'direct', [inside], 1.0),
        ]
        for index, (stream_id, kind, positives, _) in enumerate(plan):
            present = tuning.approximate_present_persons(store, stream_id, NOW)
            replay = tuning.replay_union_recall(
                store, present, '咖啡 手冲', '', 6, NOW, stream_kind=kind,
            )
            assert inside in {fact.id for fact in replay}
            assert outside not in {fact.id for fact in replay}
            _insert_retrieval_trace(
                db, turn_id=20 + index, stream_id=stream_id, at=NOW,
                current_text='咖啡 手冲', impression='',
                pool=[(fact.id, fact.score) for fact in replay],
                prompt_fact_ids=positives,
            )
        report = await tuning.evaluate_round2(
            store, tuning.build_trace_samples(db), [], {},
        )
        groups = report.traced['groups']
        assert groups['all']['sample_count'] == 3
        assert groups['all']['scorable_count'] == 3
        assert groups['all']['ndcg_mean'] == pytest.approx(2 / 3, abs=1e-4)
        assert groups['all']['displaced_positive_total'] == 1
        assert groups['group']['sample_count'] == 2
        assert groups['group']['ndcg_mean'] == pytest.approx(0.5, abs=1e-4)
        assert groups['group']['displaced_positive_total'] == 1
        assert groups['direct']['sample_count'] == 1
        assert groups['direct']['ndcg_mean'] == pytest.approx(1.0)
        assert groups['direct']['displaced_positive_total'] == 0


class TestReadonlyAndRestore:
    """评估只读、覆盖表还原。"""

    async def test_evaluation_restores_overrides_and_writes_nothing(self, db):
        store = _make_store(db)
        _seed_stream(db, 101, 'direct')
        _seed_message(db, 101, OWNER_PERSON_ID, '聊聊咖啡', NOW - 1_000)
        coffee = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他只喝手冲咖啡', kind='偏好', origin_kind='direct'),
            NOW,
        ).fact_id
        present = tuning.approximate_present_persons(store, 101, NOW)
        replay = tuning.replay_union_recall(
            store, present, '咖啡 手冲', '', 6, NOW, stream_kind='direct',
        )
        _insert_retrieval_trace(
            db, turn_id=30, stream_id=101, at=NOW,
            current_text='咖啡 手冲', impression='',
            pool=[(fact.id, fact.score) for fact in replay],
            prompt_fact_ids=[coffee],
        )
        tuning.set_active_overrides({'ppr_alpha': 0.9})
        try:
            await tuning.evaluate_round2(
                store, tuning.build_trace_samples(db), [],
                {'fact_recall_limit': 4},
            )
        finally:
            assert tuning.active_overrides() == {'ppr_alpha': 0.9}
        tuning.set_active_overrides({})
        row = db.execute(
            'SELECT hit_count, strength FROM facts WHERE id = ?', (coffee,)
        ).fetchone()
        assert row[0] == 0
