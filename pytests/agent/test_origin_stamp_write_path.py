"""来源标记收进写入路径的机检断言（★O-1 ~ ★O-5）。

来源标记曾是「落库后补写」的临时接线（_stamp_origin_kind + 批尾额外 commit），
本文件钉住它收进 ``MemoryStore.add_fact`` 之后的形态：与正文同一事务、
升格语义唯一、旧符号消失、生产路径永不落 legacy。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import List

import pytest

from src.core.agent import fact_extract
from src.core.agent.fact_extract import ExtractedFact, Participant, persist_facts
from src.core.memory.scope import ORIGIN_DIRECT, ORIGIN_GROUP, ORIGIN_LEGACY
from src.core.memory.store import FactInput, MemoryStore

OWNER_PERSON_ID = 1
NOW = 1_800_000_000_000
SRC_ROOT = Path(fact_extract.__file__).resolve().parent.parent.parent


def _people(store: MemoryStore, db) -> List[Participant]:
    """一名归属明确的在场者，与 test_fact_extract 的替身口径一致。"""

    return [Participant(external_id='900000001', display_name='他', person_id=OWNER_PERSON_ID)]


def _origin_of(db, fact_id: int) -> str:
    row = db.execute('SELECT origin_kind FROM facts WHERE id = ?', (fact_id,)).fetchone()
    assert row is not None
    return str(row[0])


class TestOriginStampSameTransaction:
    """★O-1：新建行的来源与正文在同一事务里落库。"""

    def test_success_writes_origin_with_content(self, db):
        store = MemoryStore(db)

        result = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他喜欢喝冰美式', kind='偏好', origin_kind=ORIGIN_DIRECT),
            NOW,
        )

        assert result.created is True
        assert _origin_of(db, result.fact_id) == ORIGIN_DIRECT

    def test_mid_write_failure_leaves_no_default_origin_row(self, db, monkeypatch):
        """正文 INSERT 已执行、事务提交前失败：不留下「正文在、来源是默认值」的行。"""

        store = MemoryStore(db)

        def explode(content: str) -> str:
            raise RuntimeError('injected mid-transaction failure')

        # index_tokens 在 INSERT facts 之后、facts_fts 参数求值时调用：注入点
        # 恰好落在「正文已写入、事务未提交」的窗口里。
        monkeypatch.setattr('src.core.memory.store.index_tokens', explode)

        with pytest.raises(RuntimeError, match='injected'):
            store.add_fact(
                OWNER_PERSON_ID,
                FactInput(content='他喜欢喝冰美式', kind='偏好', origin_kind=ORIGIN_DIRECT),
                NOW,
            )

        db.rollback()
        count = db.execute(
            "SELECT COUNT(*) FROM facts WHERE content = '他喜欢喝冰美式'"
        ).fetchone()[0]
        assert count == 0


class TestOriginPromotionMatrix:
    """★O-2：升格只有 direct→group 一个方向，legacy 与 group 永不改写。"""

    def _seed(self, db, origin: str) -> MemoryStore:
        store = MemoryStore(db)
        store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他喜欢喝冰美式', kind='偏好', origin_kind=origin),
            NOW,
        )
        return store

    def _reinforce(self, store: MemoryStore, batch_origin: str):
        return store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他喜欢喝冰美式', kind='偏好', origin_kind=batch_origin),
            NOW + 1,
        )

    def test_direct_promoted_by_group_restatement(self, db):
        store = self._seed(db, ORIGIN_DIRECT)

        result = self._reinforce(store, ORIGIN_GROUP)

        assert _origin_of(db, result.fact_id) == ORIGIN_GROUP
        assert result.origin_promoted is True

    @pytest.mark.parametrize('batch_origin', [ORIGIN_DIRECT, ORIGIN_LEGACY])
    def test_direct_kept_when_not_group_restatement(self, db, batch_origin):
        store = self._seed(db, ORIGIN_DIRECT)

        result = self._reinforce(store, batch_origin)

        assert _origin_of(db, result.fact_id) == ORIGIN_DIRECT
        assert result.origin_promoted is False

    @pytest.mark.parametrize(
        'existing_origin,batch_origin',
        [
            (ORIGIN_GROUP, ORIGIN_DIRECT),
            (ORIGIN_GROUP, ORIGIN_GROUP),
            (ORIGIN_GROUP, ORIGIN_LEGACY),
            (ORIGIN_LEGACY, ORIGIN_DIRECT),
            (ORIGIN_LEGACY, ORIGIN_GROUP),
            (ORIGIN_LEGACY, ORIGIN_LEGACY),
        ],
    )
    def test_group_and_legacy_never_rewritten(self, db, existing_origin, batch_origin):
        store = self._seed(db, existing_origin)

        result = self._reinforce(store, batch_origin)

        assert _origin_of(db, result.fact_id) == existing_origin
        assert result.origin_promoted is False

    @pytest.mark.asyncio
    async def test_group_restatement_promotes_through_persist_facts(self, db):
        """端到端：私聊写入的 direct 事实在群聊重说后升格为 group。"""

        store = MemoryStore(db)
        people = _people(store, db)
        await persist_facts(
            store,
            [ExtractedFact('900000001', '偏好', '他喜欢喝冰美式')],
            people,
            db,
            NOW,
            origin_kind=ORIGIN_DIRECT,
        )

        written = await persist_facts(
            store,
            [ExtractedFact('900000001', '偏好', '他喜欢喝冰美式')],
            people,
            db,
            NOW + 1,
            origin_kind=ORIGIN_GROUP,
        )

        assert _origin_of(db, written[0]) == ORIGIN_GROUP


class TestOldWiringRemoved:
    """★O-3 / ★O-4：旧符号与批尾额外 commit 已消失，来源仍照常落库。"""

    def test_stamp_helpers_gone_from_source_tree(self):
        source = Path(fact_extract.__file__).read_text(encoding='utf-8')
        assert '_stamp_origin_kind' not in source
        assert 'stamped' not in source
        for py in SRC_ROOT.rglob('*.py'):
            assert '_stamp_origin_kind' not in py.read_text(encoding='utf-8'), str(py)

    def test_persist_facts_has_no_extra_commit(self):
        source = inspect.getsource(persist_facts)
        assert 'db.commit' not in source

    @pytest.mark.asyncio
    async def test_origin_lands_without_extra_commit(self, db):
        store = MemoryStore(db)
        people = _people(store, db)

        written = await persist_facts(
            store,
            [ExtractedFact('900000001', '偏好', '他喜欢喝冰美式')],
            people,
            db,
            NOW,
            origin_kind=ORIGIN_DIRECT,
        )

        assert _origin_of(db, written[0]) == ORIGIN_DIRECT


class TestProductionNeverLegacy:
    """★O-5：persist_facts 写出的事实永远不是 legacy。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize('origin', [ORIGIN_DIRECT, ORIGIN_GROUP])
    async def test_persisted_rows_carry_declared_origin(self, db, origin):
        store = MemoryStore(db)
        people = _people(store, db)

        await persist_facts(
            store,
            [ExtractedFact('900000001', '偏好', '他喜欢喝冰美式')],
            people,
            db,
            NOW,
            origin_kind=origin,
        )

        legacy_rows = db.execute(
            'SELECT COUNT(*) FROM facts WHERE origin_kind = ?', (ORIGIN_LEGACY,)
        ).fetchone()[0]
        assert legacy_rows == 0

    def test_run_extraction_declares_origin_explicitly(self):
        """生产链路的调用点必须显式映射会话类型，不许落默认值。"""

        source = inspect.getsource(fact_extract)
        assert 'origin_kind=origin_kind_for_stream(stream_kind)' in source
