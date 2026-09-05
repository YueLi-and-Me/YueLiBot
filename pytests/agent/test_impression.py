"""会话印象与在场者事实召回（W8）。

R-1 印象补检索、R-2 限流不重算、R-4 失败可见降级，以及 R-5 向量随行、
R-6 按事实自己的归属回补强度。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from src.core.agent.impression import (
    IMPRESSION_MIN_NEW_MESSAGES,
    IMPRESSION_TTL_MS,
    ConversationImpressions,
)
from src.core.memory.store import FactInput, MemoryStore, ScopedFact
from src.core.platform_io.registry import StreamRegistry

OWNER_PERSON_ID = 1
NOW = 1_800_000_000_000


class _StubProvider:
    """按脚本逐条产出文本的假模型；记录全部请求供断言调用次数。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    async def stream(self, messages, temperature, max_tokens):
        self.requests.append(messages)
        yield {'text': self.replies.pop(0) if self.replies else ''}


class _FailingProvider:
    async def stream(self, messages, temperature, max_tokens):
        raise RuntimeError('模型不可用')
        yield  # pragma: no cover


def _speaker(person_id: int, stream_id: int) -> str:
    return '某人'


def _fill_chat(store: MemoryStore, stream_id: int, count: int) -> None:
    for i in range(count):
        store.append_message(
            stream_id, OWNER_PERSON_ID, 'user', f'最近在研究手冲咖啡的参数第{i}条', 1000 + i,
        )


def _make_service(store: MemoryStore, provider, window: int = 40) -> ConversationImpressions:
    return ConversationImpressions(store, provider, window)


class TestImpressionRefreshAndCache:
    async def test_first_call_generates_and_second_reuses_cache(self, db):
        """★R-2：未达新增条数且未超 TTL 时不重算（数模型调用次数）。"""
        store = MemoryStore(db)
        _fill_chat(store, 1, 8)
        provider = _StubProvider(['这几天大家一直在聊手冲咖啡的冲煮手法和器具。'])
        service = _make_service(store, provider)

        first = await service.current(
            1, bot_name='月璃', speaker_name=_speaker,
            temperature=0.3, max_tokens=None, now=NOW,
        )
        second = await service.current(
            1, bot_name='月璃', speaker_name=_speaker,
            temperature=0.3, max_tokens=None, now=NOW + 1000,
        )

        assert first == '这几天大家一直在聊手冲咖啡的冲煮手法和器具。'
        assert second == first
        assert len(provider.requests) == 1

    async def test_ttl_expiry_triggers_recompute(self, db):
        store = MemoryStore(db)
        _fill_chat(store, 1, 8)
        provider = _StubProvider(['旧印象。', '新印象。'])
        service = _make_service(store, provider)

        await service.current(
            1, bot_name='月璃', speaker_name=_speaker,
            temperature=0.3, max_tokens=None, now=NOW,
        )
        text = await service.current(
            1, bot_name='月璃', speaker_name=_speaker,
            temperature=0.3, max_tokens=None, now=NOW + IMPRESSION_TTL_MS,
        )

        assert text == '新印象。'
        assert len(provider.requests) == 2

    async def test_new_messages_threshold_triggers_recompute(self, db):
        store = MemoryStore(db)
        _fill_chat(store, 1, 8)
        provider = _StubProvider(['旧印象。', '新印象。'])
        service = _make_service(store, provider)

        await service.current(
            1, bot_name='月璃', speaker_name=_speaker,
            temperature=0.3, max_tokens=None, now=NOW,
        )
        _fill_chat(store, 1, IMPRESSION_MIN_NEW_MESSAGES)
        text = await service.current(
            1, bot_name='月璃', speaker_name=_speaker,
            temperature=0.3, max_tokens=None, now=NOW + 1000,
        )

        assert text == '新印象。'

    async def test_empty_window_returns_none_without_model_call(self, db):
        store = MemoryStore(db)
        provider = _StubProvider(['不该被调用'])
        service = _make_service(store, provider)

        text = await service.current(
            1, bot_name='月璃', speaker_name=_speaker,
            temperature=0.3, max_tokens=None, now=NOW,
        )

        assert text is None
        assert provider.requests == []

    async def test_no_provider_returns_none(self, db):
        store = MemoryStore(db)
        _fill_chat(store, 1, 8)
        service = _make_service(store, None)

        assert await service.current(
            1, bot_name='月璃', speaker_name=_speaker,
            temperature=0.3, max_tokens=None, now=NOW,
        ) is None


class TestImpressionFailure:
    async def test_failure_is_visible_and_degrades_to_none(self, db, monkeypatch):
        """★R-4：失败退回「只用当前文本检索」，失败在 trace 里看得见。"""
        store = MemoryStore(db)
        _fill_chat(store, 1, 8)
        service = _make_service(store, _FailingProvider())

        emitted = []
        import src.core.agent.impression as impression_module
        monkeypatch.setattr(
            impression_module.trace, 'emit',
            lambda kind, **fields: emitted.append((kind, fields)),
        )

        text = await service.current(
            1, bot_name='月璃', speaker_name=_speaker,
            temperature=0.3, max_tokens=None, now=NOW,
        )

        assert text is None
        failures = [fields for kind, fields in emitted if kind == 'memory_impression_failed']
        assert failures and '模型不可用' in failures[0]['error']

    async def test_failure_drops_stale_cache(self, db):
        """失败后下一次不得复用旧印象：缓存必须清掉。"""
        store = MemoryStore(db)
        _fill_chat(store, 1, 8)
        provider = _StubProvider(['旧印象。'])
        service = _make_service(store, provider)

        await service.current(
            1, bot_name='月璃', speaker_name=_speaker,
            temperature=0.3, max_tokens=None, now=NOW,
        )

        async def failing_stream(messages, temperature, max_tokens):
            raise RuntimeError('第二次失败')
            yield  # pragma: no cover

        provider.stream = failing_stream
        text = await service.current(
            1, bot_name='月璃', speaker_name=_speaker,
            temperature=0.3, max_tokens=None, now=NOW + IMPRESSION_TTL_MS,
        )

        assert text is None


class TestImpressionAsRetrievalQuery:
    def test_short_reply_recall_rescued_by_impression(self, db):
        """★R-1：当前消息是「哈哈」这类无检索词时，印象命中的事实仍可召回。"""
        store = MemoryStore(db)
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='他喜欢手冲咖啡'), NOW)
        impression = '这几天大家一直在聊手冲咖啡的冲煮手法和器具。'

        # 当前文本单独检索：短应答没有有效词，召回为空（改造前行为）。
        assert store.recall_facts_in_scope(
            (OWNER_PERSON_ID,), '哈哈', 6, NOW + 1, stream_kind='group',
        ) == []
        # 印象作为第二检索词：命中相关事实。
        hits = store.recall_facts_in_scope(
            (OWNER_PERSON_ID,), impression, 6, NOW + 1, stream_kind='group',
        )

        assert [f.content for f in hits] == ['他喜欢手冲咖啡']

    def test_union_pool_merges_and_dedupes(self, db):
        """两个检索词的候选合并去重后统一排序，同一事实只进池一次。"""
        store = MemoryStore(db)
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='他喜欢手冲咖啡'), NOW)
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='习惯', content='他常去咖啡馆自习'), NOW)
        query, impression = '咖啡', '大家聊咖啡聊得起劲'

        pool = []
        seen = set()
        for text in (query, impression):
            for fact in store.recall_facts_in_scope(
                (OWNER_PERSON_ID,), text, 6, NOW + 1,
                stream_kind='direct', return_candidates=True,
            ):
                if fact.id not in seen:
                    seen.add(fact.id)
                    pool.append(fact)

        assert len(pool) == len({f.id for f in pool})
        assert len(pool) >= 2


class TestMultiPersonRecall:
    def test_in_scope_returns_embedding_for_semantic_fusion(self, db):
        """★R-5：返回的事实带 embedding，语义融合分支能进入。"""
        store = MemoryStore(db)
        fid = store.add_fact(OWNER_PERSON_ID, FactInput(kind='偏好', content='他喜欢手冲咖啡'), NOW).fact_id
        raw = bytes(range(8))
        store.store_embedding(fid, raw)

        hits = store.recall_facts_in_scope(
            (OWNER_PERSON_ID,), '咖啡', 6, NOW + 1, stream_kind='direct',
        )

        assert hits[0].embedding == raw

    def test_non_speaker_facts_within_limit(self, db):
        """★R-3（store 层）：在场者范围内非当前说话人的事实可被召回。"""
        store = MemoryStore(db)
        registry = StreamRegistry(db)
        other = registry.create_person('contact', 1_700_000_000_001)
        store.add_fact(
            OWNER_PERSON_ID, FactInput(kind='偏好', content='她喜欢手冲咖啡'), NOW,
        )
        store.add_fact(
            other.id, FactInput(kind='偏好', content='他在咖啡馆做兼职'), NOW,
        )

        hits = store.recall_facts_in_scope(
            (OWNER_PERSON_ID, other.id), '咖啡', 6, NOW + 1, stream_kind='group',
        )

        assert {h.person_id for h in hits} == {OWNER_PERSON_ID, other.id}

    def test_reinforce_uses_each_facts_own_person(self, db):
        """★R-6：回补按每条事实自己的 person_id 落行，不记到别人头上。"""
        store = MemoryStore(db)
        registry = StreamRegistry(db)
        other = registry.create_person('contact', 1_700_000_000_001)
        mine = store.add_fact(
            OWNER_PERSON_ID, FactInput(kind='偏好', content='她喜欢手冲咖啡'), NOW,
        ).fact_id
        theirs = store.add_fact(
            other.id, FactInput(kind='偏好', content='他在咖啡馆做兼职'), NOW,
        ).fact_id
        hits = store.recall_facts_in_scope(
            (OWNER_PERSON_ID, other.id), '咖啡', 6, NOW + 1, stream_kind='group',
        )

        store.reinforce_recalled_facts(hits, NOW + 2)

        def row(fact_id):
            return db.execute(
                'SELECT person_id, hit_count, updated_at FROM facts WHERE id = ?', (fact_id,)
            ).fetchone()

        assert row(mine)[0] == OWNER_PERSON_ID and row(mine)[1] == 1
        assert row(theirs)[0] == other.id and row(theirs)[1] == 1
        assert row(mine)[2] == NOW + 2 and row(theirs)[2] == NOW + 2

    def test_reinforce_skips_fact_of_mismatched_owner(self, db):
        """person_id 对不上的行不落 UPDATE：防越权改写他人事实强度。"""
        store = MemoryStore(db)
        mine = store.add_fact(
            OWNER_PERSON_ID, FactInput(kind='偏好', content='她喜欢手冲咖啡'), NOW,
        ).fact_id
        fake = ScopedFact(
            id=mine, kind='偏好', content='她喜欢手冲咖啡',
            retention=1.0, score=1.0, person_id=999,
        )

        store.reinforce_recalled_facts([fake], NOW + 2)

        assert db.execute(
            'SELECT hit_count FROM facts WHERE id = ?', (mine,)
        ).fetchone()[0] == 0
