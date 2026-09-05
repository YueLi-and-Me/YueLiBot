"""事实账本（W9）：槽位冲突、显式取代与失效过滤。

覆盖任务书的四条机检断言：同槽异值并存且带冲突标记（★L-1）、显式取代后旧行
不再被任何召回入口返回（★L-3）、空槽位多值事实永不判冲突（★L-4）、完全重复
仍只强化不新增（★L-6）。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.memory.store import FactInput, MemoryStore
from src.core.platform_io.registry import StreamRegistry

NOW = 1_800_000_000_000
DAY = 86_400_000
OWNER_PERSON_ID = 1


@pytest.fixture
def store():
    db = sqlite3.connect(':memory:')
    store = MemoryStore(db)
    yield store
    db.close()


class TestSlotConflicts:
    def test_l1_same_slot_distinct_values_coexist_and_are_flagged(self, store):
        """同一槽位出现异值：不覆盖、不取舍，两条都活着且都被标进冲突组。"""

        first = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        )
        assert first.created and first.conflict_with == []

        second = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地'),
            NOW + 1,
        )
        assert second.fact_id != first.fact_id
        assert second.conflict_with == [first.fact_id]

        third = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在深圳', kind='身份', slot='居住地'),
            NOW + 2,
        )
        assert third.conflict_with == [first.fact_id, second.fact_id]

        # 三条都可查到，且被归入同一个冲突组；成员列表含全组，供提示词并排渲染。
        groups = store.slot_conflicts(
            OWNER_PERSON_ID, [first.fact_id, second.fact_id, third.fact_id]
        )
        assert set(groups) == {first.fact_id, second.fact_id, third.fact_id}
        slot, members = groups[first.fact_id]
        assert slot == '居住地'
        assert [content for _, content in members] == [
            '他现在住在成都',
            '他现在住在杭州',
            '他现在住在深圳',
        ]

    def test_l1_conflict_group_includes_members_not_in_the_input(self, store):
        """只传入组内一个成员时，返回仍带全组——只给一半等于没给。"""

        first = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        )
        second = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地'),
            NOW + 1,
        )

        groups = store.slot_conflicts(OWNER_PERSON_ID, [second.fact_id])

        assert [member_id for member_id, _ in groups[second.fact_id][1]] == [
            first.fact_id,
            second.fact_id,
        ]

    def test_l4_multi_value_facts_without_slot_never_conflict(self, store):
        """slot 为空的多值事实之间永不判冲突。"""

        first = store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他喜欢喝美式', kind='偏好'), NOW
        )
        second = store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他喜欢喝拿铁', kind='偏好'), NOW + 1
        )

        assert first.conflict_with == [] and second.conflict_with == []
        assert store.slot_conflicts(OWNER_PERSON_ID, [first.fact_id, second.fact_id]) == {}

    def test_frozen_fact_does_not_conflict(self, store):
        """冲突组只统计活跃事实：已冻结的旧值不与新值并列。"""

        old = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        )
        assert store.sweep(NOW + 10000 * DAY) == 1

        new = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地'),
            NOW + 10000 * DAY,
        )

        assert new.conflict_with == []
        assert store.slot_conflicts(OWNER_PERSON_ID, [new.fact_id]) == {}
        assert store.slot_conflicts(OWNER_PERSON_ID, [old.fact_id]) == {}

    def test_l6_exact_duplicate_still_only_reinforces(self, store):
        """回归护栏：完全重复的事实仍然只加强、不新增，即便带着槽位。"""

        first = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他喜欢喝美式', kind='偏好', slot='口味'),
            NOW,
        )
        again = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他喜欢喝美式', kind='偏好', slot='口味'),
            NOW + 1,
        )

        assert again.fact_id == first.fact_id
        assert again.created is False
        assert again.conflict_with == []
        assert store.fact_count(OWNER_PERSON_ID)['total'] == 1


class TestSupersede:
    def test_l3_explicit_supersede_retires_old_row_from_every_recall_entry(self, store):
        """显式取代：旧行 superseded_by 非空，且不再被任何召回入口返回。"""

        old = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        )
        new = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地', supersedes=old.fact_id),
            NOW + 1,
        )

        assert new.superseded == old.fact_id
        # 旧行已失效，不与新行构成冲突。
        assert new.conflict_with == []
        row = store._db.execute(
            'SELECT superseded_by FROM facts WHERE id = ?', (old.fact_id,)
        ).fetchone()
        assert row[0] == new.fact_id

        # 三个召回入口都不再返回旧行：查新值得新行，查旧值得到空。
        for recalled in (
            store.recall_facts(OWNER_PERSON_ID, '杭州', 5, NOW + 2, stream_kind='direct'),
            store.recall_facts_in_scope((OWNER_PERSON_ID,), '杭州', 5, NOW + 2, stream_kind='direct'),
            store.top_facts(OWNER_PERSON_ID, 5, NOW + 2, stream_kind='direct'),
        ):
            assert [fact.id for fact in recalled] == [new.fact_id]
        assert store.recall_facts(OWNER_PERSON_ID, '成都', 5, NOW + 2, stream_kind='direct') == []
        assert store.recall_facts_in_scope((OWNER_PERSON_ID,), '成都', 5, NOW + 2, stream_kind='direct') == []

    def test_supersede_requires_same_person(self, store):
        """越界的取代不能落到别人的事实上。"""

        registry = StreamRegistry(store._db)
        other = registry.create_person('contact', NOW)
        mine = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        )
        theirs = store.add_fact(
            other.id,
            FactInput(content='她现在住在拉萨', kind='身份', slot='居住地'),
            NOW,
        )

        new = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地', supersedes=theirs.fact_id),
            NOW + 1,
        )

        assert new.superseded == 0
        row = store._db.execute(
            'SELECT superseded_by FROM facts WHERE id = ?', (theirs.fact_id,)
        ).fetchone()
        assert row[0] is None
        # 取代被拒不影响冲突检测：新行与本人同槽旧行仍然对不上。
        assert new.conflict_with == [mine.fact_id]

    def test_supersede_twice_is_rejected(self, store):
        """已被取代的行不能再被取代一次——取代链不允许分叉。"""

        old = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        )
        first = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地', supersedes=old.fact_id),
            NOW + 1,
        )
        second = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在深圳', kind='身份', slot='居住地', supersedes=old.fact_id),
            NOW + 2,
        )

        assert first.superseded == old.fact_id
        assert second.superseded == 0
        row = store._db.execute(
            'SELECT superseded_by FROM facts WHERE id = ?', (old.fact_id,)
        ).fetchone()
        assert row[0] == first.fact_id

    def test_supersede_self_is_rejected(self, store):
        """同文重提并声明取代自己时，只强化，不形成自指。"""

        old = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他喜欢喝美式', kind='偏好'),
            NOW,
        )
        again = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他喜欢喝美式', kind='偏好', supersedes=old.fact_id),
            NOW + 1,
        )

        assert again.fact_id == old.fact_id
        assert again.created is False
        assert again.superseded == 0
        row = store._db.execute(
            'SELECT superseded_by FROM facts WHERE id = ?', (old.fact_id,)
        ).fetchone()
        assert row[0] is None

    def test_supersede_survives_duplicate_content(self, store):
        """新正文恰好与既有事实同义时，取代声明仍然落到被声明的旧行上。"""

        old = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        )
        existing = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地'),
            NOW + 1,
        )

        # 模型用另一种措辞重述「杭州」并声明取代「成都」：内容命中既有行，
        # 只强化不新增；取代仍应指向那条既有行。
        result = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地', supersedes=old.fact_id),
            NOW + 2,
        )

        assert result.fact_id == existing.fact_id
        assert result.created is False
        assert result.superseded == old.fact_id
        row = store._db.execute(
            'SELECT superseded_by FROM facts WHERE id = ?', (old.fact_id,)
        ).fetchone()
        assert row[0] == existing.fact_id
