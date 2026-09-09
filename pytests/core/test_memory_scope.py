"""跨会话事实可见性（W7）。

覆盖可见性矩阵、legacy 逐字节等价、入口必填参数、拦截事件与写入侧来源标记。
判据：记忆的可见范围由它被听见的场合决定。
"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.agent.fact_extract import ExtractedFact, Participant, persist_facts
from src.core.memory.scope import (
    ORIGIN_DIRECT,
    ORIGIN_GROUP,
    ORIGIN_LEGACY,
    fact_visible_in_stream,
    origin_kind_for_stream,
)
from src.core.memory.store import EpisodeInput, FactInput, MemoryStore
from src.core.platform_io.registry import StreamRegistry

OWNER_PERSON_ID = 1
NOW = 1_800_000_000_000


class TestVisibilityMatrix:
    """★S-1：direct 不进群聊，开关打开后出现；其余场合全可见。"""

    def test_direct_fact_hidden_in_group_until_switch(self):
        assert fact_visible_in_stream(ORIGIN_DIRECT, 'group') is False
        assert fact_visible_in_stream(ORIGIN_DIRECT, 'group', private_in_group=True) is True

    def test_direct_fact_visible_in_direct_and_desktop(self):
        assert fact_visible_in_stream(ORIGIN_DIRECT, 'direct') is True
        assert fact_visible_in_stream(ORIGIN_DIRECT, 'desktop') is True

    def test_group_and_legacy_visible_everywhere(self):
        for stream_kind in ('group', 'direct', 'desktop'):
            assert fact_visible_in_stream(ORIGIN_GROUP, stream_kind) is True
            assert fact_visible_in_stream(ORIGIN_LEGACY, stream_kind) is True

    def test_unknown_origin_follows_legacy_behavior(self):
        assert fact_visible_in_stream('???', 'group') is True

    def test_origin_mapping_for_write_side(self):
        assert origin_kind_for_stream('group') == ORIGIN_GROUP
        assert origin_kind_for_stream('direct') == ORIGIN_DIRECT
        assert origin_kind_for_stream('desktop') == ORIGIN_DIRECT


class _RecallHarness:
    """三个读取入口共用的建库与断言工具。"""

    def __init__(self, db: sqlite3.Connection):
        self.store = MemoryStore(db)
        self.db = db
        self.registry = StreamRegistry(db)
        self.fact_id = self.store.add_fact(
            OWNER_PERSON_ID, FactInput(kind='偏好', content='喜欢手冲咖啡'), NOW,
        ).fact_id

    def set_origin(self, origin_kind: str) -> None:
        self.db.execute(
            'UPDATE facts SET origin_kind = ? WHERE id = ?', (origin_kind, self.fact_id)
        )


class TestEntryPointsEnforceVisibility:
    def test_direct_fact_blocked_in_group_recall(self, db):
        h = _RecallHarness(db)
        h.set_origin(ORIGIN_DIRECT)

        assert h.store.recall_facts(
            OWNER_PERSON_ID, '咖啡', 6, NOW + 1, stream_kind='group',
        ) == []
        assert h.store.recall_facts_in_scope(
            (OWNER_PERSON_ID,), '咖啡', 6, NOW + 1, stream_kind='group',
        ) == []
        assert h.store.top_facts(
            OWNER_PERSON_ID, 8, NOW + 1, stream_kind='group',
        ) == []

    def test_switch_reveals_direct_fact_in_group(self, db):
        h = _RecallHarness(db)
        h.set_origin(ORIGIN_DIRECT)

        assert h.store.recall_facts(
            OWNER_PERSON_ID, '咖啡', 6, NOW + 1,
            stream_kind='group', private_in_group=True,
        )
        assert h.store.recall_facts_in_scope(
            (OWNER_PERSON_ID,), '咖啡', 6, NOW + 1,
            stream_kind='group', private_in_group=True,
        )
        assert h.store.top_facts(
            OWNER_PERSON_ID, 8, NOW + 1,
            stream_kind='group', private_in_group=True,
        )

    def test_unknown_stream_kinds_do_not_block_direct_facts(self, db):
        """私聊与桌面不是群聊，direct 来源照常可见。"""
        h = _RecallHarness(db)
        h.set_origin(ORIGIN_DIRECT)

        for kind in ('direct', 'desktop'):
            assert h.store.recall_facts(
                OWNER_PERSON_ID, '咖啡', 6, NOW + 1, stream_kind=kind,
            )


class TestLegacyEquivalence:
    """★S-2：legacy 行的可见性与改造前逐字节相同。

    对照方式：同一库里放一条 legacy 行，用读取入口（带 stream_kind）取回的
    结果与未上可见性时的既有行为——即「按查询命中、按词面分排序、截断到
    limit」——完全一致。过滤开启前后 legacy 命中集合不变。
    """

    def test_legacy_rows_match_unfiltered_baseline(self, db):
        store = MemoryStore(db)
        contents = ['他喜欢手冲咖啡', '他在学 Rust', '他养了一只猫']
        for content in contents:
            fid = store.add_fact(OWNER_PERSON_ID, FactInput(kind='习惯', content=content), NOW).fact_id
            # 全部保持默认 legacy。
            row = db.execute(
                'SELECT origin_kind FROM facts WHERE id = ?', (fid,)
            ).fetchone()
            assert row[0] == ORIGIN_LEGACY

        for stream_kind in ('group', 'direct', 'desktop'):
            recall = [f.content for f in store.recall_facts(
                OWNER_PERSON_ID, '咖啡 猫', 3, NOW + 1, stream_kind=stream_kind,
            )]
            in_scope = [f.content for f in store.recall_facts_in_scope(
                (OWNER_PERSON_ID,), '咖啡 猫', 3, NOW + 1, stream_kind=stream_kind,
            )]
            top = [f.content for f in store.top_facts(
                OWNER_PERSON_ID, 3, NOW + 1, stream_kind=stream_kind,
            )]
            assert recall == in_scope
            assert sorted(top) == sorted(contents)


class TestBlockedTrace:
    """★S-5：被挡下的条数进 trace，数量与构造一致。"""

    def test_blocked_count_matches_constructed_facts(self, db, monkeypatch):
        store = MemoryStore(db)
        # 3 条 direct + 1 条 group 来源，同被「咖啡」命中。
        for content, origin in (
            ('他喜欢手冲咖啡', ORIGIN_DIRECT),
            ('他常去咖啡馆自习', ORIGIN_DIRECT),
            ('他买了咖啡豆', ORIGIN_DIRECT),
            ('他请群里的人喝了咖啡', ORIGIN_GROUP),
        ):
            fid = store.add_fact(OWNER_PERSON_ID, FactInput(kind='习惯', content=content), NOW).fact_id
            db.execute(
                'UPDATE facts SET origin_kind = ? WHERE id = ?', (origin, fid)
            )

        emitted = []
        monkeypatch.setattr(
            'src.core.memory.store.trace.emit',
            lambda kind, **fields: emitted.append((kind, fields)),
        )
        hits = store.recall_facts(OWNER_PERSON_ID, '咖啡', 6, NOW + 1, stream_kind='group')

        blocked = [fields for kind, fields in emitted if kind == 'memory_fact_scope_blocked']
        assert blocked == [{'streamKind': 'group', 'blocked': 3}]
        assert [f.content for f in hits] == ['他请群里的人喝了咖啡']

    def test_no_event_when_nothing_blocked(self, db, monkeypatch):
        store = MemoryStore(db)
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='习惯', content='他喜欢手冲咖啡'), NOW)
        emitted = []
        monkeypatch.setattr(
            'src.core.memory.store.trace.emit',
            lambda kind, **fields: emitted.append((kind, fields)),
        )

        store.recall_facts(OWNER_PERSON_ID, '咖啡', 6, NOW + 1, stream_kind='direct')

        assert [kind for kind, _ in emitted if kind == 'memory_fact_scope_blocked'] == []


class TestStreamKindRequired:
    """★S-3：不传 stream_kind 无法调用。"""

    def test_all_entries_reject_missing_stream_kind(self, db):
        store = MemoryStore(db)
        store.add_fact(OWNER_PERSON_ID, FactInput(kind='习惯', content='他喜欢手冲咖啡'), NOW)

        with pytest.raises(TypeError):
            store.recall_facts(OWNER_PERSON_ID, '咖啡', 6, NOW + 1)
        with pytest.raises(TypeError):
            store.recall_facts_in_scope((OWNER_PERSON_ID,), '咖啡', 6, NOW + 1)
        with pytest.raises(TypeError):
            store.recall_facts_across_persons('咖啡', 6, NOW + 1)
        with pytest.raises(TypeError):
            store.top_facts(OWNER_PERSON_ID, 8, NOW + 1)


class TestCrossPersonRecallEntry:
    """★K2：跨人物读取入口只认可见性，不认「在不在场」。

    放开人物范围的会话门在服务层（非群聊 + owner）；入口本身必须保证
    ``superseded_by`` 与可见性过滤一道不缺，被挡条数照常落账。
    """

    def _absent_person_with_fact(
        self, db, store: MemoryStore, content: str, origin: str,
    ) -> int:
        """构造一个只在群里出现过的人物及其一条事实，返回事实 ID。"""

        registry = StreamRegistry(db)
        context = registry.resolve_inbound(
            platform='qq',
            stream_kind='group',
            stream_external_id='629201002',
            sender_external_id='3209184542',
            sender_nickname='不思量デス',
            sender_group_card='不思量',
            first_seen_at=NOW,
        )
        fact_id = store.add_fact(
            context.person.id, FactInput(kind='事件', content=content), NOW,
        ).fact_id
        db.execute(
            'UPDATE facts SET origin_kind = ? WHERE id = ?', (origin, fact_id)
        )
        return fact_id

    def test_cross_person_entry_hits_absent_person_group_fact(self, db):
        """★K2-1 正例：私聊里不在场人物的 group 来源事实能被命中。"""

        store = MemoryStore(db)
        self._absent_person_with_fact(db, store, '3209184542 玩《原神》这款游戏', ORIGIN_GROUP)

        hits = store.recall_facts_across_persons('原神', 6, NOW + 1, stream_kind='direct')

        assert [fact.content for fact in hits] == ['3209184542 玩《原神》这款游戏']
        assert hits[0].person_id != OWNER_PERSON_ID
        # 对照：同一查询按在场者范围取，命中数为 0。
        assert store.recall_facts_in_scope(
            (OWNER_PERSON_ID,), '原神', 6, NOW + 1, stream_kind='direct',
        ) == []

    def test_cross_person_entry_keeps_visibility_rules_in_group(self, db, monkeypatch):
        """★K2-2：跨人物入口在群聊里仍挡 direct 来源，W7 不回退。"""

        store = MemoryStore(db)
        self._absent_person_with_fact(db, store, '3209184542 玩《原神》这款游戏', ORIGIN_DIRECT)
        emitted = []
        monkeypatch.setattr(
            'src.core.memory.store.trace.emit',
            lambda kind, **fields: emitted.append((kind, fields)),
        )

        blocked_hits = store.recall_facts_across_persons('原神', 6, NOW + 1, stream_kind='group')

        assert blocked_hits == []
        blocked = [fields for kind, fields in emitted if kind == 'memory_fact_scope_blocked']
        assert blocked == [{'streamKind': 'group', 'blocked': 1}]
        # 同一条事实换到非群聊会话就可见：挡它的是场合，不是人物范围。
        assert store.recall_facts_across_persons('原神', 6, NOW + 1, stream_kind='direct')

    def test_cross_person_entry_excludes_superseded_facts(self, db):
        """已被取代的事实不进入跨人物候选，与在场者入口同口径。"""

        store = MemoryStore(db)
        fact_id = self._absent_person_with_fact(
            db, store, '3209184542 玩《原神》这款游戏', ORIGIN_GROUP,
        )
        replacement = store.add_fact(
            OWNER_PERSON_ID, FactInput(kind='事件', content='他改玩别的游戏了'), NOW,
        ).fact_id
        db.execute('UPDATE facts SET superseded_by = ? WHERE id = ?', (replacement, fact_id))

        assert store.recall_facts_across_persons('原神', 6, NOW + 1, stream_kind='direct') == []


class TestEpisodeIsolation:
    """★S-4：情节召回仍严格按 stream 隔离。"""

    def test_cross_stream_episode_recall_is_empty(self, db):
        store = MemoryStore(db)
        registry = StreamRegistry(db)
        group = registry.get_or_create_stream('qq', 'group', '12345')
        # 桌面 stream 写入一条带线索的情节，再从群 stream 检索同一关键词。
        store.add_episode(1, EpisodeInput(
            summary='他最近在聊熬夜与作息的事',
            cues=['他提到熬夜的时候'],
            started_at=1000, ended_at=2000, message_ids=[],
        ))

        assert store.recall_episodes(group.id, '熬夜') == []
        assert len(store.recall_episodes(1, '熬夜')) == 1


class TestWriteSideOrigin:
    """写入侧：persist_facts 落来源标记；私聊来源被群聊重说时升格。"""

    @pytest.mark.asyncio
    async def test_new_facts_get_stream_origin(self, db):
        store = MemoryStore(db)
        people = [Participant(external_id='10001', display_name='某人', person_id=OWNER_PERSON_ID)]

        written = await persist_facts(
            store,
            [ExtractedFact('10001', '偏好', '他喜欢手冲咖啡')],
            people,
            db,
            NOW,
            origin_kind=ORIGIN_GROUP,
        )

        row = db.execute(
            'SELECT origin_kind FROM facts WHERE id = ?', (written[0],)
        ).fetchone()
        assert row[0] == ORIGIN_GROUP

    @pytest.mark.asyncio
    async def test_public_repeat_upgrades_direct_to_group(self, db):
        store = MemoryStore(db)
        people = [Participant(external_id='10001', display_name='某人', person_id=OWNER_PERSON_ID)]

        first = await persist_facts(
            store,
            [ExtractedFact('10001', '偏好', '他喜欢手冲咖啡')],
            people, db, NOW, origin_kind=ORIGIN_DIRECT,
        )
        # 同一事实在群里被重说：add_fact 判定相似后强化旧行，来源升格为 group。
        second = await persist_facts(
            store,
            [ExtractedFact('10001', '偏好', '他喜欢手冲咖啡')],
            people, db, NOW + 60_000, origin_kind=ORIGIN_GROUP,
        )

        assert second[0] == first[0]
        row = db.execute(
            'SELECT origin_kind FROM facts WHERE id = ?', (first[0],)
        ).fetchone()
        assert row[0] == ORIGIN_GROUP

    @pytest.mark.asyncio
    async def test_group_origin_never_downgrades(self, db):
        store = MemoryStore(db)
        people = [Participant(external_id='10001', display_name='某人', person_id=OWNER_PERSON_ID)]

        first = await persist_facts(
            store,
            [ExtractedFact('10001', '偏好', '他喜欢手冲咖啡')],
            people, db, NOW, origin_kind=ORIGIN_GROUP,
        )
        await persist_facts(
            store,
            [ExtractedFact('10001', '偏好', '他喜欢手冲咖啡')],
            people, db, NOW + 60_000, origin_kind=ORIGIN_DIRECT,
        )

        row = db.execute(
            'SELECT origin_kind FROM facts WHERE id = ?', (first[0],)
        ).fetchone()
        assert row[0] == ORIGIN_GROUP
