"""认知动作依赖的两个只读检索接口。

覆盖 recall_facts_in_scope 的跨人物范围与「不回补强度」，以及 search_messages
的命中排序、水位边界与扫描上界。这些是 recall / inspect 的全部数据来源，
标定就靠这几条撑着。
"""

from __future__ import annotations

from src.core.memory.store import FactInput, MemoryStore
from src.core.platform_io.registry import StreamRegistry

DESKTOP_STREAM_ID = 1
OWNER_PERSON_ID = 1


class TestRecallFactsInScope:
    def test_covers_multiple_persons_in_one_query(self, db):
        store = MemoryStore(db)
        registry = StreamRegistry(db)
        other = registry.create_person('contact', 1_700_000_000_000)
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='喜欢手冲咖啡'))
        store.add_fact(other.id, FactInput(kind='偏好', content='喜欢喝美式咖啡'))

        hits = store.recall_facts_in_scope(
            (OWNER_PERSON_ID, other.id), '咖啡', stream_kind='direct',
        )

        assert {hit.person_id for hit in hits} == {OWNER_PERSON_ID, other.id}

    def test_empty_scope_returns_nothing(self, db):
        store = MemoryStore(db)
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='喜欢手冲咖啡'))

        assert store.recall_facts_in_scope((), '咖啡', stream_kind='direct') == []

    def test_scope_excludes_persons_outside_it(self, db):
        store = MemoryStore(db)
        registry = StreamRegistry(db)
        other = registry.create_person('contact', 1_700_000_000_000)
        store.add_fact(other.id, FactInput(kind='偏好', content='喜欢喝美式咖啡'))

        assert store.recall_facts_in_scope(
            (OWNER_PERSON_ID,), '咖啡', stream_kind='direct',
        ) == []

    def test_does_not_reinforce_matches(self, db):
        """决策期检索不得改写遗忘曲线。

        她「想了一下」这个动作本身若参与强化，同一条事实被反复 recall 就再也不会
        衰减，遗忘曲线形同虚设。写回只应发生在真实使用时。
        """
        store = MemoryStore(db)
        fact_id = store.add_fact(
            OWNER_PERSON_ID, FactInput(kind='偏好', content='喜欢手冲咖啡'),
        ).fact_id
        before = db.execute(
            'SELECT strength, updated_at, hit_count FROM facts WHERE id = ?', (fact_id,),
        ).fetchone()

        assert store.recall_facts_in_scope(
            (OWNER_PERSON_ID,), '咖啡', stream_kind='direct',
        )

        after = db.execute(
            'SELECT strength, updated_at, hit_count FROM facts WHERE id = ?', (fact_id,),
        ).fetchone()
        assert tuple(after) == tuple(before)


class TestSearchMessages:
    def _seed(self, store: MemoryStore) -> list[int]:
        return [
            store.append_message(DESKTOP_STREAM_ID, OWNER_PERSON_ID, 'user', '周末去看了演出', 1000),
            store.append_message(DESKTOP_STREAM_ID, None, 'assistant', '好玩吗', 2000),
            store.append_message(DESKTOP_STREAM_ID, OWNER_PERSON_ID, 'user', '演出很赞，乐队也很稳', 3000),
            store.append_message(DESKTOP_STREAM_ID, OWNER_PERSON_ID, 'user', '今天在写代码', 4000),
        ]

    def test_returns_hits_in_chronological_order(self, db):
        store = MemoryStore(db)
        ids = self._seed(store)

        hits = store.search_messages(DESKTOP_STREAM_ID, '演出', before_id=ids[-1] + 1)

        assert [hit.message_id for hit in hits] == [ids[0], ids[2]]

    def test_ranks_by_matched_term_count(self, db):
        store = MemoryStore(db)
        ids = self._seed(store)

        # 「演出 乐队」两个词都命中的那条必须入选；只给 1 条名额时它优先。
        hits = store.search_messages(
            DESKTOP_STREAM_ID, '演出 乐队', before_id=ids[-1] + 1, limit=1,
        )

        assert [hit.message_id for hit in hits] == [ids[2]]

    def test_watermark_excludes_later_messages(self, db):
        """水位之后的消息不进检索，认知轮看到的东西才不会随新消息漂移。"""
        store = MemoryStore(db)
        ids = self._seed(store)

        hits = store.search_messages(DESKTOP_STREAM_ID, '演出', before_id=ids[2])

        assert [hit.message_id for hit in hits] == [ids[0]]

    def test_other_streams_are_not_visible(self, db):
        store = MemoryStore(db)
        registry = StreamRegistry(db)
        group = registry.get_or_create_stream('qq', 'group', '12345')
        store.append_message(group.id, OWNER_PERSON_ID, 'user', '演出真好看', 1000)

        assert store.search_messages(DESKTOP_STREAM_ID, '演出', before_id=10_000) == []

    def test_query_without_terms_returns_nothing(self, db):
        store = MemoryStore(db)
        self._seed(store)

        # 纯标点分词后没有有效词，不能退化成「捞最近若干条」。
        assert store.search_messages(DESKTOP_STREAM_ID, '？？？', before_id=10_000) == []


class TestRecentSpeakers:
    def test_lists_distinct_speakers_most_recent_first(self, db):
        store = MemoryStore(db)
        registry = StreamRegistry(db)
        other = registry.create_person('contact', 1_700_000_000_000)
        store.append_message(DESKTOP_STREAM_ID, OWNER_PERSON_ID, 'user', '甲', 1000)
        store.append_message(DESKTOP_STREAM_ID, None, 'assistant', '乙', 2000)
        last = store.append_message(DESKTOP_STREAM_ID, other.id, 'user', '丙', 3000)

        assert store.recent_speakers(DESKTOP_STREAM_ID, last) == [other.id, OWNER_PERSON_ID]

    def test_watermark_bounds_the_scope(self, db):
        store = MemoryStore(db)
        registry = StreamRegistry(db)
        other = registry.create_person('contact', 1_700_000_000_000)
        first = store.append_message(DESKTOP_STREAM_ID, OWNER_PERSON_ID, 'user', '甲', 1000)
        store.append_message(DESKTOP_STREAM_ID, other.id, 'user', '丙', 3000)

        assert store.recent_speakers(DESKTOP_STREAM_ID, first) == [OWNER_PERSON_ID]
