"""检索调优中心的机检断言。

覆盖：参数白名单的拒绝语义、nDCG 手算值、blend 与现状打分的等价性、
profile 的保存/应用/回滚/导出/删除生命周期、评估的参数生效与还原、
候选池百分位过滤。评估集构建（build_turn_samples）依赖事件账本与转储
文件，其端到端正确性由真机库副本验收覆盖，此处只测纯函数件。
"""

from __future__ import annotations

import pytest

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


class TestEvaluate:
    """评估：参数生效、只读、结束后还原。"""

    def test_evaluate_runs_and_restores(self, db):
        store = MemoryStore(db)
        fid = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他喜欢喝冰美式咖啡', kind='偏好', origin_kind='direct'),
            NOW,
        ).fact_id
        sample = tuning.TurnSample(
            turn_id=1,
            stream_id=1,
            stream_kind='direct',
            at=NOW + 1,
            query='美式咖啡',
            present_person_ids=[OWNER_PERSON_ID],
            positive_fact_ids=[fid],
        )
        tuning.set_active_overrides({'ppr_alpha': 0.9})
        try:
            report = tuning.evaluate(
                store, [sample], {'fact_recall_limit': 3}, config_fact_limit=6,
            )
        finally:
            assert tuning.active_overrides() == {'ppr_alpha': 0.9}
        tuning.set_active_overrides({})

        assert report.k == 3
        assert report.sample_count == 1
        assert report.per_turn[0]['recall_count'] >= 1
        assert report.per_turn[0]['ndcg'] > 0.0
        # 评估重放不改强度：事实只读。
        row = db.execute(
            'SELECT hit_count FROM facts WHERE id = ?', (fid,)
        ).fetchone()
        assert row[0] == 0

    def test_evaluate_rejects_bad_params(self, db):
        with pytest.raises(ValueError, match='不在调优白名单'):
            tuning.evaluate(MemoryStore(db), [], {'nope': 1})


class TestFactMatching:
    """渲染条目行匹配回事实 ID 的纯函数件。"""

    def test_match_by_content(self, db):
        store = MemoryStore(db)
        fid = store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他喜欢喝冰美式', kind='偏好'), NOW
        ).fact_id
        lines = ['炫芽衣 他喜欢喝冰美式']
        assert tuning._match_fact_ids(db, lines) == [fid]

    def test_unmatched_line_dropped(self, db):
        assert tuning._match_fact_ids(db, ['某人 完全无关的正文']) == []
