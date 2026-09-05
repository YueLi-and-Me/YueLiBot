"""记忆的人工管理（curate）回归。

覆盖验收 M-1~M-7：标失效/恢复对三个召回入口的可见性、流水 prev 快照、
撤销回滚、冲突裁决、永久保留抗衰减，以及 manual 与 n4 两类 actor 的区分；
另补撤销取代（new_row_created=true）回滚新行的用例。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.core.memory import curate
from src.core.memory.decay import (
    MS_PER_HOUR,
    PIN_HALF_LIFE_HOURS,
    half_life_for,
    is_pinned,
    retention,
)
from src.core.memory.store import FactInput, MemoryStore

NOW = 1_800_000_000_000
OWNER_PERSON_ID = 1


@pytest.fixture
def store():
    db = sqlite3.connect(':memory:')
    store = MemoryStore(db)
    yield store
    db.close()


def _decay_fields(row: dict) -> dict:
    """取行字典里的衰减五字段，便于整体断言。"""

    return {key: row[key] for key in ('strength', 'half_life_hours', 'updated_at', 'due_at', 'active')}


class TestInvalidateAndRestore:
    def test_m1_invalidated_fact_leaves_every_recall_entry(self, store):
        """M-1：标失效后 recall_facts / recall_facts_in_scope / top_facts 都召不回。"""

        fact = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地'),
            NOW,
        )
        curate.invalidate_fact(store, fact.fact_id, NOW + 1)

        assert store.recall_facts(
            OWNER_PERSON_ID, '杭州', 5, NOW + 2, reinforce_matches=False, stream_kind='direct',
        ) == []
        assert store.recall_facts_in_scope(
            (OWNER_PERSON_ID,), '杭州', 5, NOW + 2, stream_kind='direct',
        ) == []
        assert store.top_facts(OWNER_PERSON_ID, 5, NOW + 2, stream_kind='direct') == []

    def test_m2_restored_fact_returns_with_same_retention(self, store):
        """M-2：恢复后三个入口又能召回到，且 retention 与标失效前相同。"""

        fact = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地'),
            NOW,
        )
        before = store.top_facts(OWNER_PERSON_ID, 5, NOW + 1, stream_kind='direct')
        assert [f.id for f in before] == [fact.fact_id]
        before_retention = before[0].retention

        curate.invalidate_fact(store, fact.fact_id, NOW + 2)
        curate.restore_fact(store, fact.fact_id, NOW + 3)

        recalled = store.recall_facts(
            OWNER_PERSON_ID, '杭州', 5, NOW + 1, reinforce_matches=False, stream_kind='direct',
        )
        assert [f.id for f in recalled] == [fact.fact_id]
        assert recalled[0].retention == pytest.approx(before_retention)
        scoped = store.recall_facts_in_scope(
            (OWNER_PERSON_ID,), '杭州', 5, NOW + 1, stream_kind='direct',
        )
        assert [f.id for f in scoped] == [fact.fact_id]
        assert scoped[0].retention == pytest.approx(before_retention)
        top = store.top_facts(OWNER_PERSON_ID, 5, NOW + 1, stream_kind='direct')
        assert [f.id for f in top] == [fact.fact_id]
        assert top[0].retention == pytest.approx(before_retention)

    def test_invalidate_restore_state_guards(self, store):
        """不存在与重复标失效/无效恢复分别走 404/409 语义。"""

        fact = store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他喜欢喝美式', kind='偏好'), NOW,
        )
        with pytest.raises(curate.FactNotFoundError):
            curate.invalidate_fact(store, 9999, NOW + 1)
        curate.invalidate_fact(store, fact.fact_id, NOW + 2)
        with pytest.raises(curate.FactStateError):
            curate.invalidate_fact(store, fact.fact_id, NOW + 3)
        curate.restore_fact(store, fact.fact_id, NOW + 4)
        with pytest.raises(curate.FactStateError):
            curate.restore_fact(store, fact.fact_id, NOW + 5)


class TestOperationLog:
    def test_m3_every_operation_writes_log_with_prev_snapshot(self, store):
        """M-3：每类操作都写了流水，prev 能还原操作前的值。"""

        fact = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地'),
            NOW,
        )
        original = store.fact_row(fact.fact_id)

        inv_op = curate.invalidate_fact(store, fact.fact_id, NOW + 1)
        row = store.fact_operation_row(inv_op)
        assert row['actor'] == 'manual' and row['op'] == 'invalidate'
        assert row['fact_id'] == fact.fact_id and row['person_id'] == OWNER_PERSON_ID
        assert json.loads(row['prev']) == {'superseded_by': None}

        res_op = curate.restore_fact(store, fact.fact_id, NOW + 2)
        row = store.fact_operation_row(res_op)
        assert row['op'] == 'restore'
        assert json.loads(row['prev']) == {'superseded_by': fact.fact_id}

        pin_op = curate.pin_fact(store, fact.fact_id, NOW + 3)
        row = store.fact_operation_row(pin_op)
        assert row['op'] == 'pin'
        assert json.loads(row['prev']) == _decay_fields(original)

        pinned = store.fact_row(fact.fact_id)
        unpin_op = curate.unpin_fact(store, fact.fact_id, NOW + 4)
        row = store.fact_operation_row(unpin_op)
        assert row['op'] == 'unpin'
        assert json.loads(row['prev']) == _decay_fields(pinned)
        assert json.loads(row['prev'])['half_life_hours'] == PIN_HALF_LIFE_HOURS

        written = curate.replace_fact(store, fact.fact_id, '他现在住在深圳', NOW + 5)
        assert written.created and written.operation_id
        row = store.fact_operation_row(written.operation_id)
        assert row['op'] == 'replace' and row['actor'] == 'manual'
        assert row['fact_id'] == fact.fact_id
        assert row['related_fact_id'] == written.fact_id
        assert json.loads(row['prev']) == {'superseded_by': None, 'new_row_created': True}

    def test_m7_manual_and_n4_operations_distinguishable_by_actor(self, store):
        """M-7：manual invalidate 与 n4 取代各写一条流水，可按 actor 区分。"""

        old = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        )
        inv_op = curate.invalidate_fact(store, old.fact_id, NOW + 1)
        curate.restore_fact(store, old.fact_id, NOW + 2)  # 救回来，N4 才能取代它
        written = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(
                content='他现在住在杭州', kind='身份', slot='居住地',
                supersedes=old.fact_id, actor='n4',
            ),
            NOW + 3,
        )
        assert written.superseded == old.fact_id

        ops = {op['id']: op for op in curate.operation_log(store, fact_id=old.fact_id)}
        assert ops[inv_op]['actor'] == 'manual' and ops[inv_op]['op'] == 'invalidate'
        n4_op = ops[written.operation_id]
        assert n4_op['actor'] == 'n4' and n4_op['op'] == 'supersede'
        assert n4_op['related_fact_id'] == written.fact_id
        assert n4_op['prev'] == {'superseded_by': None, 'new_row_created': True}
        assert n4_op['fact_content'] == '他现在住在成都'


class TestUndo:
    def test_m4_undo_pin_and_invalidate_restore_row_state(self, store):
        """M-4：对 pin 和 invalidate 各做一次 undo，行回到操作前状态。"""

        fact = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地'),
            NOW,
        )
        original = store.fact_row(fact.fact_id)

        pin_op = curate.pin_fact(store, fact.fact_id, NOW + 1)
        assert is_pinned(store.fact_row(fact.fact_id)['half_life_hours'])
        undo_id = curate.undo_operation(store, pin_op, NOW + 2)
        assert _decay_fields(store.fact_row(fact.fact_id)) == _decay_fields(original)

        # 撤销链互链：原条 undone_by 指向新条，新条 undo_of 指回原条。
        assert store.fact_operation_row(pin_op)['undone_by'] == undo_id
        undo_row = store.fact_operation_row(undo_id)
        assert undo_row['op'] == 'undo' and undo_row['undo_of'] == pin_op
        assert undo_row['related_fact_id'] == fact.fact_id

        inv_op = curate.invalidate_fact(store, fact.fact_id, NOW + 3)
        assert store.fact_row(fact.fact_id)['superseded_by'] == fact.fact_id
        curate.undo_operation(store, inv_op, NOW + 4)
        row = store.fact_row(fact.fact_id)
        assert row['superseded_by'] is None
        # 撤销不改变留存强度之外的语义：invalidate/undo 往返不动衰减四字段。
        assert _decay_fields(row) == _decay_fields(original)

    def test_undo_supersede_invalidates_new_row_and_revives_old(self, store):
        """撤销 new_row_created=true 的取代：新行标失效、旧行回到活跃。"""

        old = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        )
        written = curate.replace_fact(store, old.fact_id, '他现在住在杭州', NOW + 1)
        assert store.fact_row(old.fact_id)['superseded_by'] == written.fact_id
        assert store.recall_facts(
            OWNER_PERSON_ID, '成都', 5, NOW + 2, reinforce_matches=False, stream_kind='direct',
        ) == []

        curate.undo_operation(store, written.operation_id, NOW + 3)

        assert store.fact_row(old.fact_id)['superseded_by'] is None
        # 撤销产生的新行只标自指失效，不物理删除。
        assert store.fact_row(written.fact_id)['superseded_by'] == written.fact_id
        recalled = store.recall_facts(
            OWNER_PERSON_ID, '成都', 5, NOW + 4, reinforce_matches=False, stream_kind='direct',
        )
        assert [f.id for f in recalled] == [old.fact_id]
        assert store.recall_facts(
            OWNER_PERSON_ID, '杭州', 5, NOW + 4, reinforce_matches=False, stream_kind='direct',
        ) == []

    def test_undo_rejects_already_undone_and_undo_ops(self, store):
        """已撤销的操作与撤销操作本身不能再撤销（409 语义）；流水不存在走 404。"""

        fact = store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他喜欢喝美式', kind='偏好'), NOW,
        )
        inv_op = curate.invalidate_fact(store, fact.fact_id, NOW + 1)
        undo_id = curate.undo_operation(store, inv_op, NOW + 2)

        with pytest.raises(curate.FactStateError):
            curate.undo_operation(store, inv_op, NOW + 3)
        with pytest.raises(curate.FactStateError):
            curate.undo_operation(store, undo_id, NOW + 4)
        with pytest.raises(curate.FactNotFoundError):
            curate.undo_operation(store, 9999, NOW + 5)


class TestConflicts:
    def test_m5_adjudicate_leaves_one_active_member_and_restores(self, store):
        """M-5：同槽两条活跃事实构成冲突组，裁决后组解散，被废弃行可恢复回来。"""

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

        groups = curate.conflict_groups(store, OWNER_PERSON_ID, NOW + 2)
        assert len(groups) == 1
        assert groups[0]['person_id'] == OWNER_PERSON_ID
        assert groups[0]['slot'] == '居住地'
        assert [m['id'] for m in groups[0]['members']] == [first.fact_id, second.fact_id]

        op_id = curate.adjudicate(store, second.fact_id, first.fact_id, NOW + 3)
        op = store.fact_operation_row(op_id)
        assert op['op'] == 'adjudicate' and op['actor'] == 'manual'
        assert op['fact_id'] == first.fact_id
        assert op['related_fact_id'] == second.fact_id
        assert json.loads(op['prev']) == {'superseded_by': None}

        assert curate.conflict_groups(store, OWNER_PERSON_ID, NOW + 4) == []
        assert store.fact_row(first.fact_id)['superseded_by'] == first.fact_id
        assert store.fact_row(second.fact_id)['superseded_by'] is None

        curate.restore_fact(store, first.fact_id, NOW + 5)
        groups = curate.conflict_groups(store, OWNER_PERSON_ID, NOW + 6)
        assert len(groups) == 1
        assert [m['id'] for m in groups[0]['members']] == [first.fact_id, second.fact_id]

    def test_adjudicate_rejects_mismatched_pairs(self, store):
        """同人同槽且均活跃才可裁决；参数不合法 400，状态不允许 409。"""

        first = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        )
        other_slot = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他是一名程序员', kind='身份', slot='职业'),
            NOW + 1,
        )
        with pytest.raises(ValueError):
            curate.adjudicate(store, first.fact_id, first.fact_id, NOW + 2)
        with pytest.raises(ValueError):
            curate.adjudicate(store, first.fact_id, other_slot.fact_id, NOW + 2)
        with pytest.raises(curate.FactNotFoundError):
            curate.adjudicate(store, first.fact_id, 9999, NOW + 2)

        second = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地'),
            NOW + 2,
        )
        curate.invalidate_fact(store, first.fact_id, NOW + 3)
        with pytest.raises(curate.FactStateError):
            curate.adjudicate(store, second.fact_id, first.fact_id, NOW + 4)


class TestPin:
    def test_m6_pinned_fact_survives_sweep_beyond_five_half_lives(self, store):
        """M-6：pin 一条偏好后推进 5 倍原半衰期以上，仍 active=1 且 retention ≥ 0.99。"""

        fact = store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他喜欢喝美式', kind='偏好'), NOW,
        )
        original_half_life = half_life_for('偏好')
        curate.pin_fact(store, fact.fact_id, NOW + 1)

        later = NOW + 1 + int(original_half_life * 5.5 * MS_PER_HOUR)
        store.sweep(later)

        row = store.fact_row(fact.fact_id)
        assert row['active'] == 1
        assert is_pinned(row['half_life_hours'])
        # 半衰期推远后 sweep 连评估窗口都没到。
        assert row['due_at'] > later
        current = retention(row['strength'], row['updated_at'], row['half_life_hours'], later)
        assert current >= 0.99

    def test_unpin_restores_natural_half_life(self, store):
        """取消永久保留后半衰期回到类型自然值，强度不动。"""

        fact = store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他喜欢喝美式', kind='偏好'), NOW,
        )
        curate.pin_fact(store, fact.fact_id, NOW + 1)
        curate.unpin_fact(store, fact.fact_id, NOW + 2)

        row = store.fact_row(fact.fact_id)
        assert row['half_life_hours'] == half_life_for('偏好')
        assert row['strength'] == 1.0
        with pytest.raises(curate.FactStateError):
            curate.unpin_fact(store, fact.fact_id, NOW + 3)


class TestReplace:
    def test_replace_rejects_empty_or_same_content(self, store):
        """新正文为空或与原事实同义时不写任何行。"""

        fact = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        )
        with pytest.raises(ValueError):
            curate.replace_fact(store, fact.fact_id, '   ', NOW + 1)
        with pytest.raises(ValueError):
            curate.replace_fact(store, fact.fact_id, '他现在住在成都', NOW + 1)
        assert store.fact_row(fact.fact_id)['superseded_by'] is None

        curate.invalidate_fact(store, fact.fact_id, NOW + 2)
        with pytest.raises(curate.FactStateError):
            curate.replace_fact(store, fact.fact_id, '他现在住在杭州', NOW + 3)

    def test_replace_rejects_content_key_of_an_invalid_row(self, store):
        """新正文撞上一条已失效行的去重键时拒绝：否则取代结果会落进死行召不回。"""

        dead = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        )
        live = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在杭州', kind='身份', slot='居住地'),
            NOW + 1,
        )
        curate.invalidate_fact(store, dead.fact_id, NOW + 2)

        with pytest.raises(curate.FactStateError):
            curate.replace_fact(store, live.fact_id, '他现在住在成都', NOW + 3)

        # 恢复撞键行后同一取代放行：旧行失效，正文落在被强化的既有行上。
        curate.restore_fact(store, dead.fact_id, NOW + 4)
        written = curate.replace_fact(store, live.fact_id, '他现在住在成都', NOW + 5)
        assert written.fact_id == dead.fact_id and written.created is False
        assert store.fact_row(live.fact_id)['superseded_by'] == dead.fact_id
        op = store.fact_operation_row(written.operation_id)
        assert op['op'] == 'replace'
        assert json.loads(op['prev']) == {'superseded_by': None, 'new_row_created': False}
