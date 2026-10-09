"""检索调优中心的机检断言。

覆盖：参数白名单的拒绝语义、nDCG 手算值、blend 与现状打分的等价性、
profile 的保存/应用/回滚/导出/删除生命周期、评估的参数生效、评估与线上
召回并发时的参数隔离、候选池百分位过滤。留痕样本构建、对账与分组报告见
``test_retrieval_tuning_round2.py``。
"""

from __future__ import annotations

import asyncio
import json
import pytest
import threading

from src.core.api.http import _retrieval_eval_run
from src.core.db.connection import run_in_thread
from src.core.memory import tuning
from src.core.memory.store import FactInput, MemoryStore

OWNER_PERSON_ID = 1
NOW = 1_800_000_000_000


class TestWhitelist:
    """白名单外的参数必须被拒绝。"""

    def test_unknown_name_rejected(self):
        with pytest.raises(ValueError, match='不在调优白名单'):
            tuning.validate_overrides({'freeze_threshold': 0.2})

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError, match='越界'):
            tuning.validate_overrides({'bm25_weight': 99.0})

    def test_int_param_rejects_float(self):
        with pytest.raises(ValueError, match='需要整数'):
            tuning.validate_overrides({'ppr_hops': 1.5})

    def test_bool_rejected(self):
        with pytest.raises(ValueError, match='需要数值'):
            tuning.validate_overrides({'ppr_alpha': True})

    def test_legal_overrides_pass(self):
        got = tuning.validate_overrides({'bm25_weight': 2.0, 'ppr_hops': 3})
        assert got == {'bm25_weight': 2.0, 'ppr_hops': 3}


class TestBlendScore:
    """打分注入：默认等价现状，覆盖可改变结果。"""

    def test_default_matches_legacy_formula(self):
        tuning.set_active_overrides({})
        try:
            for relevance, retention in [(0.0, 0.0), (0.5, 0.3), (0.9, 1.0)]:
                assert tuning.blend_score(relevance, retention) == pytest.approx(
                    relevance * (0.35 + 0.65 * retention)
                )
        finally:
            tuning.set_active_overrides({})

    def test_floor_override_changes_low_retention_ranking(self):
        try:
            tuning.set_active_overrides({})
            before = tuning.blend_score(0.8, 0.0)
            tuning.set_active_overrides({'retention_weight_floor': 0.8})
            after = tuning.blend_score(0.8, 0.0)
            assert after > before
        finally:
            tuning.set_active_overrides({})

    def test_recall_responds_to_bm25_weight(self, db):
        """权重调大后，词面差异对排序的影响应当放大。"""

        store = MemoryStore(db)
        near = store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他喜欢喝冰美式咖啡', kind='偏好'), NOW
        ).fact_id
        far = store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他去咖啡馆自习', kind='习惯'), NOW
        ).fact_id
        try:
            tuning.set_active_overrides({})
            baseline = [
                fact.id for fact in store.recall_facts_in_scope(
                    [OWNER_PERSON_ID], '咖啡', 6, NOW, stream_kind='direct',
                )
            ]
            tuning.set_active_overrides({'bm25_weight': 4.0})
            boosted = [
                fact.id for fact in store.recall_facts_in_scope(
                    [OWNER_PERSON_ID], '咖啡', 6, NOW, stream_kind='direct',
                )
            ]
        finally:
            tuning.set_active_overrides({})
        # 两条都含「咖啡」，都在召回池里；词面更贴合的必须在前。
        assert near in baseline and far in baseline
        assert near in boosted and far in boosted
        assert baseline.index(near) < baseline.index(far)
        assert boosted.index(near) < boosted.index(far)


class TestNdcg:
    """nDCG@k 的手算校验。"""

    def test_hand_computed_value(self):
        import math

        ranked = [10, 11, 12, 13]
        positives = {11, 13}
        dcg = 1 / math.log2(3) + 1 / math.log2(5)
        idcg = 1 / math.log2(2) + 1 / math.log2(3)
        assert tuning.ndcg_at_k(ranked, positives, 4) == pytest.approx(dcg / idcg)

    def test_perfect_ranking_scores_one(self):
        assert tuning.ndcg_at_k([5, 6, 7], {5, 6}, 3) == pytest.approx(1.0)

    def test_empty_positives_scores_zero(self):
        assert tuning.ndcg_at_k([1, 2, 3], [], 3) == 0.0

    def test_truncation_at_k(self):
        """正例全在 k 之外时得零分：截断深度必须真的截断。"""
        assert tuning.ndcg_at_k([1, 2, 3, 4], {4}, 3) == 0.0


class TestPoolPercentile:
    """候选池分数百分位过滤。"""

    def test_zero_keeps_all(self):
        try:
            tuning.set_active_overrides({})
            assert tuning.apply_pool_percentile([0.9, 0.5, 0.1]) == 3
        finally:
            tuning.set_active_overrides({})

    def test_half_percentile_cuts_tail(self):
        try:
            tuning.set_active_overrides({'pool_score_percentile': 0.5})
            assert tuning.apply_pool_percentile([0.9, 0.5, 0.1]) == 2
        finally:
            tuning.set_active_overrides({})


class TestProfileLifecycle:
    """save / list / apply / rollback / export / delete。"""

    def test_full_lifecycle(self, db):
        tuning.set_active_overrides({})
        tuning.save_profile(db, 'boost', {'bm25_weight': 2.0}, NOW)
        assert any(p['name'] == 'boost' for p in tuning.list_profiles(db))

        applied = tuning.apply_profile(db, 'boost', NOW + 1)
        assert applied['params'] == {'bm25_weight': 2.0}
        assert tuning.active_profile_name(db) == 'boost'
        assert tuning.active_overrides() == {'bm25_weight': 2.0}

        exported = tuning.export_profile(db, 'boost')
        assert exported['params'] == {'bm25_weight': 2.0}

        tuning.rollback_to_default(db)
        assert tuning.active_profile_name(db) == 'default'
        assert tuning.active_overrides() == {}

        tuning.delete_profile(db, 'boost')
        assert all(p['name'] != 'boost' for p in tuning.list_profiles(db))

    def test_save_rejects_default_and_empty(self, db):
        with pytest.raises(ValueError, match='不可保存'):
            tuning.save_profile(db, 'default', {'ppr_hops': 3}, NOW)
        with pytest.raises(ValueError, match='不可保存'):
            tuning.save_profile(db, '  ', {}, NOW)

    def test_apply_missing_profile_raises(self, db):
        with pytest.raises(ValueError, match='不存在'):
            tuning.apply_profile(db, 'ghost', NOW)

    def test_bootstrap_restores_active(self, db):
        tuning.save_profile(db, 'boost', {'ppr_alpha': 0.9}, NOW)
        tuning.apply_profile(db, 'boost', NOW + 1)
        tuning.set_active_overrides({})
        assert tuning.bootstrap_active(db) == 'boost'
        assert tuning.active_overrides() == {'ppr_alpha': 0.9}
        tuning.rollback_to_default(db)

    def test_delete_active_profile_falls_back(self, db):
        tuning.save_profile(db, 'temp', {'ppr_hops': 4}, NOW)
        tuning.apply_profile(db, 'temp', NOW + 1)
        tuning.delete_profile(db, 'temp')
        assert tuning.active_profile_name(db) == 'default'
        assert tuning.active_overrides() == {}


STREAM_ID = 1


def _seed_traced_turn(
    db, *, current_text: str, prompt_fact_ids: list[int], at: int = NOW + 1,
) -> None:
    """登记一个可评分的私聊回合：主人发言、一条召回留痕与一次回复。

    留痕的 ``candidatePool`` 留空：本文件只看排序与参数隔离，对账见
    ``test_retrieval_tuning_round2.py``。
    """

    db.execute(
        "INSERT OR IGNORE INTO streams (id, platform, kind, external_id)"
        " VALUES (?, 'qq', 'direct', 'ext-1')",
        (STREAM_ID,),
    )
    db.execute(
        'INSERT INTO messages (role, content, created_at, stream_id, sender_person_id)'
        " VALUES ('user', ?, ?, ?, ?)",
        (current_text, at - 1, STREAM_ID, OWNER_PERSON_ID),
    )
    events = (
        ('memory_retrieval_trace', {
            'currentText': current_text,
            'conversationImpression': '',
            'candidatePool': [],
            'promptFactIds': prompt_fact_ids,
        }),
        ('outbound_delivered', {'text': 'ok'}),
    )
    for kind, payload in events:
        db.execute(
            'INSERT INTO pipeline_events (at, stream_id, turn_id, stage, kind, payload)'
            " VALUES (?, ?, 1, '', ?, ?)",
            (at, STREAM_ID, kind, json.dumps(payload, ensure_ascii=False)),
        )
    db.commit()


class TestEvaluate:
    """评估入口的参数校验。"""

    async def test_evaluate_rejects_bad_params(self, db):
        with pytest.raises(ValueError, match='不在调优白名单'):
            await tuning.evaluate_round2(MemoryStore(db), [], {'nope': 1})


class TestEvaluateIsolation:
    """评估与线上召回并发：线上读取只看已生效 profile，重放只看草稿。"""

    ACTIVE = {'bm25_weight': 2.0}
    DRAFT = {
        'bm25_weight': 4.0,
        'retention_weight_floor': 0.9,
        'ppr_alpha': 0.3,
        'ppr_hops': 4,
        'pool_score_percentile': 0.5,
        'fact_recall_limit': 2,
    }

    @staticmethod
    def _live_reading() -> dict:
        """按线上召回各读取点（context_build / store / association）的口径取一次参数。"""

        return {
            'fact_recall_limit': tuning.tuned_value('fact_recall_limit', 6),
            'blend': tuning.blend_score(0.5, 0.2),
            'ppr_alpha': tuning.ppr_alpha(),
            'ppr_hops': tuning.ppr_hops(2),
            'pool_kept': tuning.apply_pool_percentile([0.9, 0.5, 0.1]),
        }

    @staticmethod
    def _seed_coffee_turn(db, store: MemoryStore) -> None:
        fid = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他喜欢喝冰美式咖啡', kind='偏好', origin_kind='direct'),
            NOW,
        ).fact_id
        _seed_traced_turn(db, current_text='美式咖啡', prompt_fact_ids=[fid])

    async def test_threaded_evaluate_does_not_leak_draft(self, db, monkeypatch):
        """走 WebUI 评估端点的工作线程函数：评估在线程池里跑，事件循环照常处理聊天。"""

        store = MemoryStore(db)
        self._seed_coffee_turn(db, store)
        entered = threading.Event()
        release = threading.Event()
        replay = store.recall_facts_in_scope

        def gated_recall(*args, **kwargs):
            # 停在重放入口：此刻评估已开始、尚未结束。
            entered.set()
            assert release.wait(5)
            return replay(*args, **kwargs)

        monkeypatch.setattr(store, 'recall_facts_in_scope', gated_recall)
        tuning.set_active_overrides(self.ACTIVE)
        try:
            expected = self._live_reading()
            running = asyncio.ensure_future(run_in_thread(
                _retrieval_eval_run, db, store, self.DRAFT, None, 6, False, None,
            ))
            try:
                assert await run_in_thread(entered.wait, 5)
                during = self._live_reading()
            finally:
                release.set()
            report = await running
            assert during == expected
            assert tuning.active_overrides() == self.ACTIVE
        finally:
            tuning.set_active_overrides({})
        assert report['k'] == 2
        assert report['params'] == self.DRAFT
        assert report['replayed_count'] == 1

    async def test_round2_await_does_not_leak_draft(self, db):
        """评估在 await 查询向量时让出事件循环，同线程的其他协程也不得看到草稿。"""

        store = MemoryStore(db)
        self._seed_coffee_turn(db, store)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_embed(text: str) -> bytes | None:
            entered.set()
            await release.wait()
            return None

        tuning.set_active_overrides(self.ACTIVE)
        try:
            expected = self._live_reading()
            running = asyncio.ensure_future(tuning.evaluate_round2(
                store, tuning.build_trace_samples(db), self.DRAFT,
                embed_query=gated_embed,
            ))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                during = self._live_reading()
            finally:
                release.set()
            report = await running
            assert during == expected
            assert tuning.active_overrides() == self.ACTIVE
        finally:
            tuning.set_active_overrides({})
        assert report.k == 2

    async def test_evaluate_ranks_by_draft_not_active(self, db):
        """重放排序必须按草稿参数：生效 profile 与草稿给出相反排序时，读数跟草稿走。"""

        store = MemoryStore(db)
        # 词面更贴合但已衰减的旧事实，与词面稍弱但新鲜的事实。
        stale = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他喜欢喝冰美式咖啡', kind='偏好', origin_kind='direct'),
            NOW - 90 * 24 * 3600 * 1000,
        ).fact_id
        store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他去咖啡馆自习', kind='习惯', origin_kind='direct'),
            NOW,
        )
        _seed_traced_turn(db, current_text='咖啡', prompt_fact_ids=[stale], at=NOW)
        samples = tuning.build_trace_samples(db)
        # 留存度权重下限为 0 时排序由留存度主导，为 1 时只看词面。
        tuning.set_active_overrides({'retention_weight_floor': 0.0})
        try:
            under_active = await tuning.evaluate_round2(
                store, samples, {'fact_recall_limit': 1},
            )
            under_draft = await tuning.evaluate_round2(
                store, samples, {'retention_weight_floor': 1.0, 'fact_recall_limit': 1},
            )
            under_empty = await tuning.evaluate_round2(store, samples, {})
        finally:
            tuning.set_active_overrides({})
        assert under_active.per_turn[0]['ndcg'] == 0.0
        assert under_draft.per_turn[0]['ndcg'] == 1.0
        # 空草稿表示「回到配置初值」，不得被当成「沿用生效 profile」。
        assert under_empty.params == {}
        assert under_empty.k == 6

    def test_replay_scores_follow_explicit_overrides(self, db):
        """显式覆盖表与把同一组参数设为生效时的打分逐位一致，且不受生效 profile 影响。"""

        store = MemoryStore(db)
        store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他喜欢喝冰美式咖啡', kind='偏好'), NOW
        )
        draft = {'bm25_weight': 4.0, 'retention_weight_floor': 0.6}
        try:
            tuning.set_active_overrides({})
            explicit = store.recall_facts_in_scope(
                [OWNER_PERSON_ID], '咖啡', 6, NOW, stream_kind='direct', overrides=draft,
            )
            live = store.recall_facts_in_scope(
                [OWNER_PERSON_ID], '咖啡', 6, NOW, stream_kind='direct',
            )
            reranked = store.rank_recalled_facts(explicit, None, 6, overrides=draft)
            tuning.set_active_overrides(draft)
            applied = store.recall_facts_in_scope(
                [OWNER_PERSON_ID], '咖啡', 6, NOW, stream_kind='direct',
            )
        finally:
            tuning.set_active_overrides({})
        assert [fact.score for fact in explicit] == [fact.score for fact in applied]
        assert [fact.score for fact in explicit] != [fact.score for fact in live]
        assert [fact.id for fact in reranked] == [fact.id for fact in applied]
