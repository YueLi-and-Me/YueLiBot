"""
MemoryStore 测试。移植自 src/core/memory/store.test.ts。
"""

from __future__ import annotations

import sqlite3
import pytest

from src.core.memory.decay import FREEZE, REVIVE, evaluate, freeze_due_at, reinforce, retention, score, DecayState
from src.core.memory.store import EpisodeInput, FactInput, MemoryStore
from src.core.platform_io.registry import StreamRegistry

HOUR = 3_600_000
DAY = 24 * HOUR
DESKTOP_STREAM_ID = 1
OWNER_PERSON_ID = 1


class TestDecayCurve:
    def test_half_life_at_deadline(self):
        t0 = 1_000_000
        assert abs(retention(1.0, t0, 24, t0 + 24 * HOUR) - 0.5) < 1e-6
        assert abs(retention(1.0, t0, 24, t0 + 48 * HOUR) - 0.25) < 1e-6

    def test_freeze_due_at_hits_threshold(self):
        t0 = 1_000_000
        due = freeze_due_at(1.0, t0, 24)
        assert abs(retention(1.0, t0, 24, due) - FREEZE) < 1e-6

    def test_hysteresis_active_stays_between_thresholds(self):
        between = (FREEZE + REVIVE) / 2
        assert evaluate(DecayState(strength=between, updated_at=0, half_life_hours=1e9, active=True), 0).active
        assert not evaluate(DecayState(strength=between, updated_at=0, half_life_hours=1e9, active=False), 0).active

    def test_reinforce_decreases_with_current_strength(self):
        from_low = reinforce(0.05)
        from_high = reinforce(0.9)
        assert (from_low - 0.05) > (from_high - 0.9)
        assert reinforce(1.0) <= 1.0

    def test_score_favors_better_retention(self):
        assert score(-2, 0.9) > score(-2, 0.1)

    def test_score_favors_better_bm25_match(self):
        # FTS5 的 bm25() 越相关值越小，因此 -8 应表示优于 -1 的匹配；该断言同时锁定评分方向。
        assert score(-8, 0.5) > score(-1, 0.5)
        # 词频过高导致 IDF<0（bm25 为正）时不得直接获得满分，避免高频词扭曲排序。
        assert score(0.5, 1.0) == 0.0


@pytest.fixture
def store():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    s = MemoryStore(db)
    yield s
    db.close()


class TestMemoryStore:
    def test_pending_promises_round_trip(self, store):
        promises = [{
            'intentType': 5,
            'earliestAt': 1_760_000_000_000,
            'expiresAt': 1_760_007_200_000,
            'activity': '',
            'wantsVision': False,
            'subject': '周六一起打游戏吧',
        }]
        store.save_pending_promises(promises)
        assert store.load_pending_promises() == promises

    def test_l1_working_memory_chronological(self, store):
        store.append_message(DESKTOP_STREAM_ID, OWNER_PERSON_ID, 'user', '第一句', 1000)
        store.append_message(DESKTOP_STREAM_ID, None, 'assistant', '第二句', 2000)
        contents = [m.content for m in store.working_memory(DESKTOP_STREAM_ID)]
        assert contents == ['第一句', '第二句']
        assert store.pending_count(DESKTOP_STREAM_ID) == 2

    def test_l2_episode_removes_from_working_memory(self, store):
        a = store.append_message(DESKTOP_STREAM_ID, OWNER_PERSON_ID, 'user', '甲', 1000)
        b = store.append_message(DESKTOP_STREAM_ID, None, 'assistant', '乙', 2000)
        store.append_message(DESKTOP_STREAM_ID, OWNER_PERSON_ID, 'user', '丙', 3000)
        store.add_episode(DESKTOP_STREAM_ID, EpisodeInput(
            summary='聊了甲和乙', cues=['关于甲的对话'],
            started_at=1000, ended_at=2000, message_ids=[a, b],
        ))
        assert store.pending_count(DESKTOP_STREAM_ID) == 1
        assert [m.content for m in store.working_memory(DESKTOP_STREAM_ID)] == ['丙']

    def test_l2_recall_uses_cues_not_summary(self, store):
        store.add_episode(DESKTOP_STREAM_ID, EpisodeInput(
            summary='他说了很多关于工作压力的事',
            cues=['他提到加班和熬夜的时候', '需要安慰他的时候'],
            started_at=1000, ended_at=2000, message_ids=[]))
        assert len(store.recall_episodes(DESKTOP_STREAM_ID, '熬夜')) == 1
        assert len(store.recall_episodes(DESKTOP_STREAM_ID, '压力')) == 0

    def test_l1_working_memory_is_isolated_by_stream(self, store):
        registry = StreamRegistry(store._db)
        group_stream = registry.get_or_create_stream('qq', 'group', '20001')
        store.append_message(DESKTOP_STREAM_ID, OWNER_PERSON_ID, 'user', '桌面消息', 1000)
        store.append_message(group_stream.id, OWNER_PERSON_ID, 'user', '群聊消息', 2000)

        assert [message.content for message in store.working_memory(DESKTOP_STREAM_ID)] == ['桌面消息']
        assert [message.content for message in store.working_memory(group_stream.id)] == ['群聊消息']

    def test_l3_same_fact_is_independent_between_people(self, store):
        registry = StreamRegistry(store._db)
        contact = registry.create_person('contact', 1_700_000_000_000)
        owner_id = store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='喜欢咖啡'), 0).fact_id
        contact_id = store.add_fact(contact.id, FactInput(kind='偏好', content='喜欢咖啡'), 0).fact_id

        assert owner_id != contact_id
        assert [fact.id for fact in store.recall_facts(OWNER_PERSON_ID, '咖啡', 5, 0, stream_kind='direct')] == [owner_id]
        assert [fact.id for fact in store.recall_facts(contact.id, '咖啡', 5, 0, stream_kind='direct')] == [contact_id]

    def test_l3_synonym_merge(self, store):
        id1 = store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='玩家不喜欢吃香菜'))
        id2 = store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='香菜玩家不喜欢吃'))
        assert id2.fact_id == id1.fact_id
        assert id2.created is False
        assert store.fact_count(OWNER_PERSON_ID)['total'] == 1

    def test_l3_opposite_order_not_merged(self, store):
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='玩家喜欢猫'))
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='猫喜欢玩家'))
        assert store.fact_count(OWNER_PERSON_ID)['total'] == 2

    def test_l3_minor_variation_merged(self, store):
        a = store.add_fact(OWNER_PERSON_ID, FactInput(kind='习惯', content='玩家习惯在深夜写代码'))
        b = store.add_fact(OWNER_PERSON_ID, FactInput(kind='习惯', content='玩家习惯深夜写代码。'))
        assert b.fact_id == a.fact_id

    def test_l3_recall_reinforces(self, store):
        now = 2 * DAY
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='玩家不喜欢吃香菜'), 0)
        before = store.recall_facts(OWNER_PERSON_ID, '香菜', 5, now, stream_kind='direct')[0]
        assert before.retention < 1.0
        after = store.recall_facts(OWNER_PERSON_ID, '香菜', 5, now, stream_kind='direct')[0]
        assert after.retention > before.retention

    def test_forgotten_fact_still_recalled(self, store):
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='事件', content='玩家今天心情不好'), 0)
        later = 100 * DAY
        assert store.sweep(later) == 1
        fc = store.fact_count(OWNER_PERSON_ID)
        assert fc['total'] == 1 and fc['active'] == 0
        hits = store.recall_facts(OWNER_PERSON_ID, '心情', 5, later, stream_kind='direct')
        assert len(hits) == 1 and hits[0].content == '玩家今天心情不好'

    def test_sweep_leaves_long_term_facts(self, store):
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='身份', content='玩家是后端工程师'), 0)
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='事件', content='玩家现在很累'), 0)
        assert store.sweep(100 * DAY) == 1
        fc = store.fact_count(OWNER_PERSON_ID)
        assert fc['total'] == 2 and fc['active'] == 1
        assert store.top_facts(OWNER_PERSON_ID, 5, 100 * DAY, stream_kind='direct')[0].content == '玩家是后端工程师'

    def test_first_seen_at_recorded(self, store):
        assert store.first_seen_at(OWNER_PERSON_ID) > 0

    def test_frozen_fact_in_all_facts(self, store):
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='事件', content='玩家今天心情不好'), 0)
        store.sweep(100 * DAY)
        all_f = store.all_facts(OWNER_PERSON_ID, 100 * DAY)
        assert len(all_f) == 1 and all_f[0].frozen is True


class TestPendingUtterances:
    def test_queue_and_retrieve(self, store):
        now = 1_000_000
        uid = store.queue_utterance('dream', '昨晚做了个梦', deliver_after=now, expires_at=now + DAY, now=now)
        assert uid > 0
        due = store.due_utterances(now)
        assert len(due) == 1 and due[0]['text'] == '昨晚做了个梦'

    def test_not_delivered_before_deliver_after(self, store):
        now = 1_000_000
        store.queue_utterance('dream', '早安', deliver_after=now + HOUR, expires_at=now + DAY, now=now)
        assert store.due_utterances(now) == []

    def test_expired_not_returned(self, store):
        now = 1_000_000
        store.queue_utterance('dream', '过期了', deliver_after=0, expires_at=now - 1, now=now - HOUR)
        assert store.due_utterances(now) == []

    def test_mark_delivered(self, store):
        now = 1_000_000
        uid = store.queue_utterance('proactive', '嗯', deliver_after=now, expires_at=now + DAY, now=now)
        store.mark_delivered([uid], now)
        assert store.due_utterances(now + 1) == []

    def test_has_queued_since(self, store):
        now = 1_000_000
        store.queue_utterance('dream', '梦境', deliver_after=now, expires_at=now + DAY, now=now)
        assert store.has_queued_since('dream', now - 1)
        assert not store.has_queued_since('dream', now + 1)
