"""联想扩散的会话可见性过滤。

三个读取入口（recall_facts / recall_facts_in_scope / top_facts / recall_episodes）
都强制两类读取侧规则——会话可见性，以及「这条记忆已经不作数」（取代与纠错
重建）——扩散这条通道原来一条都不走：私聊里听到的事实、别的会话的情节、已被
新值取代的旧事实、用户纠正过的情节，都能沿「一起被点亮过」的边进入当前提示词。
八条验收对应：

1. 群聊里扩散命中的 ``direct`` 事实被挡，私聊里同构造可见；
2. 属于别的 stream 的情节被挡，本 stream 的情节可见（两个方向各一条）；
3. 被挡下的节点不进 ``link_together``，相关边的强度与更新时间不变；
4. 被挡条数进 trace：事实走 ``memory_fact_scope_blocked``，情节走
   ``memory_spread`` 的 ``blockedEpisodes`` 字段；
5. ``knowledge`` 扩散命中不受影响；
6. ``HOPS = 0`` 时观察文本与改造前逐字节相同；
7. ``superseded_by`` 非空的事实被挡且不被加强，计入 ``blockedSuperseded``；
   同时踩中取代与场合两条判据时按取代归因；
8. ``needs_rebuild = 1`` 的情节在屏蔽开关打开时被挡，计入 ``blockedRebuild``；
   开关关闭时保持原行为。
"""

from __future__ import annotations

from src.core.agent.cognition import CognitiveRequest, RecallAction
from src.core.memory import association
from src.core.memory.association import link_together, node_id
from src.core.memory.knowledge import add_knowledge
from src.core.memory.scope import ORIGIN_DIRECT, ORIGIN_GROUP
from src.core.memory.store import EpisodeInput, FactInput, MemoryStore
from src.core.platform_io.registry import StreamRegistry

NOW = 1_800_000_000_000
OWNER_PERSON_ID = 1


def _action(db, store: MemoryStore, *, exclude_rebuild: bool = False) -> RecallAction:
    return RecallAction(
        store, lambda person_id, stream_id: '凌白', db,
        exclude_pending_rebuild=exclude_rebuild,
    )


def _request(stream_id: int, stream_kind: str, query: str = '咖啡') -> CognitiveRequest:
    return CognitiveRequest(
        action='recall',
        query=query,
        stream_id=stream_id,
        stream_kind=stream_kind,
        person_ids=(OWNER_PERSON_ID,),
        message_watermark=0,
    )


def _add_fact(db, store: MemoryStore, content: str, origin: str) -> int:
    fact_id = store.add_fact(
        OWNER_PERSON_ID, FactInput(kind='偏好', content=content), NOW,
    ).fact_id
    db.execute('UPDATE facts SET origin_kind = ? WHERE id = ?', (origin, fact_id))
    return fact_id


def _add_episode(store: MemoryStore, stream_id: int, summary: str) -> int:
    return store.add_episode(stream_id, EpisodeInput(
        summary=summary, cues=[], started_at=1000, ended_at=2000, message_ids=[],
    ), NOW)


def _streams(db):
    registry = StreamRegistry(db)
    return (
        registry.get_or_create_stream('qq', 'group', 'spread-visibility'),
        registry.get_or_create_stream('qq', 'direct', 'spread-visibility'),
    )


class TestSpreadFactScope:
    """★S-1：扩散命中的事实按场合规则过滤，与三个读取入口同一个判据。"""

    async def test_group_spread_blocks_direct_origin_fact(self, db):
        store = MemoryStore(db)
        group, direct = _streams(db)
        seed_id = _add_fact(db, store, '他喜欢手冲咖啡', ORIGIN_GROUP)
        hidden_id = _add_fact(db, store, '他私下在学大提琴', ORIGIN_DIRECT)
        link_together(db, [('fact', seed_id), ('fact', hidden_id)], NOW)

        action = _action(db, store)
        in_group = await action.execute(_request(group.id, 'group'))
        assert '大提琴' not in in_group.text
        # 唯一的扩散命中被挡后，整段「顺带想起来的」不出现。
        assert '顺带想起来的' not in in_group.text
        assert in_group.hit_count == 1

        # 同一构造换成私聊会话：direct 事实对私聊可见，扩散照常命中。
        in_direct = await action.execute(_request(direct.id, 'direct'))
        assert '大提琴' in in_direct.text


class TestSpreadEpisodeIsolation:
    """★S-2：扩散命中的情节严格限定本 stream，两个方向各一条。"""

    async def test_group_spread_blocks_direct_stream_episode(self, db):
        store = MemoryStore(db)
        group, direct = _streams(db)
        seed_id = _add_fact(db, store, '他喜欢手冲咖啡', ORIGIN_GROUP)
        direct_episode = _add_episode(store, direct.id, '私聊里聊过他的演唱会门票')
        group_episode = _add_episode(store, group.id, '群里聊过周末烧烤')
        link_together(db, [('fact', seed_id), ('episode', direct_episode)], NOW)
        link_together(db, [('fact', seed_id), ('episode', group_episode)], NOW)

        in_group = await _action(db, store).execute(_request(group.id, 'group'))

        assert '演唱会门票' not in in_group.text
        assert '周末烧烤' in in_group.text

    async def test_direct_spread_blocks_group_stream_episode(self, db):
        store = MemoryStore(db)
        group, direct = _streams(db)
        seed_id = _add_fact(db, store, '他喜欢手冲咖啡', ORIGIN_GROUP)
        direct_episode = _add_episode(store, direct.id, '私聊里聊过他的演唱会门票')
        group_episode = _add_episode(store, group.id, '群里聊过周末烧烤')
        link_together(db, [('fact', seed_id), ('episode', direct_episode)], NOW)
        link_together(db, [('fact', seed_id), ('episode', group_episode)], NOW)

        in_direct = await _action(db, store).execute(_request(direct.id, 'direct'))

        assert '周末烧烤' not in in_direct.text
        assert '演唱会门票' in in_direct.text


class TestBlockedHitsNotReinforced:
    """★S-3：被挡下的节点不进 link_together——每次拦截都让泄漏路径更强，
    只在渲染处过滤是拦不住的。"""

    async def test_blocked_edge_strength_and_updated_at_unchanged(self, db):
        store = MemoryStore(db)
        group, _direct = _streams(db)
        seed_id = _add_fact(db, store, '他喜欢手冲咖啡', ORIGIN_GROUP)
        hidden_id = _add_fact(db, store, '他私下在学大提琴', ORIGIN_DIRECT)
        visible_episode = _add_episode(store, group.id, '群里聊过周末烧烤')
        link_together(db, [('fact', seed_id), ('fact', hidden_id)], NOW)
        link_together(db, [('fact', seed_id), ('episode', visible_episode)], NOW)

        seed_node = node_id(db, 'fact', seed_id)
        hidden_node = node_id(db, 'fact', hidden_id)
        low, high = sorted((seed_node, hidden_node))
        before = db.execute(
            'SELECT strength, updated_at FROM memory_edges'
            ' WHERE source_id = ? AND target_id = ?',
            (low, high),
        ).fetchone()
        touching_before = db.execute(
            'SELECT COUNT(*) FROM memory_edges'
            ' WHERE source_id = ? OR target_id = ?',
            (hidden_node, hidden_node),
        ).fetchone()[0]

        result = await _action(db, store).execute(_request(group.id, 'group'))

        # 对照组真的在起作用：可见情节被采用，它与种子的边被加强过。
        assert '周末烧烤' in result.text
        episode_node = node_id(db, 'episode', visible_episode)
        elow, ehigh = sorted((seed_node, episode_node))
        strengthened = db.execute(
            'SELECT updated_at FROM memory_edges WHERE source_id = ? AND target_id = ?',
            (elow, ehigh),
        ).fetchone()
        assert strengthened[0] != NOW

        after = db.execute(
            'SELECT strength, updated_at FROM memory_edges'
            ' WHERE source_id = ? AND target_id = ?',
            (low, high),
        ).fetchone()
        assert tuple(after) == tuple(before)
        touching_after = db.execute(
            'SELECT COUNT(*) FROM memory_edges'
            ' WHERE source_id = ? OR target_id = ?',
            (hidden_node, hidden_node),
        ).fetchone()[0]
        assert touching_after == touching_before


class TestBlockedAccounting:
    """★S-4：被挡条数进 trace——事实与情节各走各的账本，数值与构造一致。"""

    async def test_blocked_counts_land_in_respective_ledgers(self, db, monkeypatch):
        store = MemoryStore(db)
        group, direct = _streams(db)
        seed_id = _add_fact(db, store, '他喜欢手冲咖啡', ORIGIN_GROUP)
        hidden_fact = _add_fact(db, store, '他私下在学大提琴', ORIGIN_DIRECT)
        hidden_episode = _add_episode(store, direct.id, '私聊里聊过他的演唱会门票')
        link_together(db, [('fact', seed_id), ('fact', hidden_fact)], NOW)
        link_together(db, [('fact', seed_id), ('episode', hidden_episode)], NOW)

        emitted = []
        monkeypatch.setattr(
            'src.core.agent.cognition.trace.emit',
            lambda kind, **fields: emitted.append((kind, fields)),
        )
        result = await _action(db, store).execute(_request(group.id, 'group'))

        assert result.hit_count == 1
        blocked = [
            fields for kind, fields in emitted
            if kind == 'memory_fact_scope_blocked'
        ]
        assert blocked == [{'streamKind': 'group', 'blocked': 1}]
        spread_events = [
            fields for kind, fields in emitted if kind == 'memory_spread'
        ]
        assert len(spread_events) == 1
        assert spread_events[0]['blockedEpisodes'] == 1
        assert spread_events[0]['spread'] == 0
        assert spread_events[0]['seeds'] == 1

    async def test_no_blocked_events_when_all_visible(self, db, monkeypatch):
        store = MemoryStore(db)
        group, _direct = _streams(db)
        seed_id = _add_fact(db, store, '他喜欢手冲咖啡', ORIGIN_GROUP)
        visible_episode = _add_episode(store, group.id, '群里聊过周末烧烤')
        link_together(db, [('fact', seed_id), ('episode', visible_episode)], NOW)

        emitted = []
        monkeypatch.setattr(
            'src.core.agent.cognition.trace.emit',
            lambda kind, **fields: emitted.append((kind, fields)),
        )
        result = await _action(db, store).execute(_request(group.id, 'group'))

        assert '周末烧烤' in result.text
        assert [
            kind for kind, _fields in emitted
            if kind == 'memory_fact_scope_blocked'
        ] == []
        spread_events = [
            fields for kind, fields in emitted if kind == 'memory_spread'
        ]
        assert spread_events[0]['blockedEpisodes'] == 0
        assert spread_events[0]['spread'] == 1


class TestSpreadKnowledgeUnfiltered:
    """★S-5：知识层全局，扩散命中不受影响。"""

    async def test_knowledge_hit_still_adopted(self, db):
        store = MemoryStore(db)
        group, _direct = _streams(db)
        seed_id = _add_fact(db, store, '他喜欢手冲咖啡', ORIGIN_GROUP)
        knowledge_id = add_knowledge(db, '手冲咖啡的水温通常在九十度上下', 'test', NOW)
        link_together(db, [('fact', seed_id), ('knowledge', knowledge_id)], NOW)

        result = await _action(db, store).execute(_request(group.id, 'group'))

        assert '水温' in result.text
        # 条数与改造前一致：种子事实 1 条 + 知识扩散命中 1 条。
        assert result.hit_count == 2


class TestHopsZeroUnchanged:
    """★S-6：扩散整层关闭时，观察文本与改造前逐字节相同。"""

    async def test_hops_zero_renders_seeds_only(self, db, monkeypatch):
        store = MemoryStore(db)
        group, direct = _streams(db)
        seed_id = _add_fact(db, store, '他喜欢手冲咖啡', ORIGIN_GROUP)
        hidden_fact = _add_fact(db, store, '他私下在学大提琴', ORIGIN_DIRECT)
        hidden_episode = _add_episode(store, direct.id, '私聊里聊过他的演唱会门票')
        link_together(db, [('fact', seed_id), ('fact', hidden_fact)], NOW)
        link_together(db, [('fact', seed_id), ('episode', hidden_episode)], NOW)

        real_spread = association.spread

        def spread_off(db_, seeds, now, *, activation=None):
            return real_spread(db_, seeds, now, hops=0, activation=activation)

        monkeypatch.setattr('src.core.agent.cognition.spread', spread_off)
        result = await _action(db, store).execute(_request(group.id, 'group'))

        assert result.text == (
            '关于「咖啡」，你想起这些：\n'
            '- 你记得关于凌白：他喜欢手冲咖啡'
        )
        assert result.hit_count == 1


class TestSupersededFactBlocked:
    """★S-7：被新事实取代的旧值不进扩散——取代只回填 superseded_by 不动
    active，而扩散只看 active，旧值因此能绕开三个读取入口的
    ``superseded_by IS NULL`` 复活。"""

    async def test_superseded_fact_not_rendered_and_not_reinforced(self, db):
        store = MemoryStore(db)
        _group, direct = _streams(db)
        seed_id = _add_fact(db, store, '他喜欢手冲咖啡', ORIGIN_GROUP)
        stale_id = _add_fact(db, store, '他在星野公司上班', ORIGIN_DIRECT)
        fresh_id = _add_fact(db, store, '他现在在云岚公司上班', ORIGIN_DIRECT)
        db.execute(
            'UPDATE facts SET superseded_by = ? WHERE id = ?', (fresh_id, stale_id),
        )
        link_together(db, [('fact', seed_id), ('fact', stale_id)], NOW)

        seed_node = node_id(db, 'fact', seed_id)
        stale_node = node_id(db, 'fact', stale_id)
        low, high = sorted((seed_node, stale_node))
        before = db.execute(
            'SELECT strength, updated_at FROM memory_edges'
            ' WHERE source_id = ? AND target_id = ?',
            (low, high),
        ).fetchone()

        # 私聊会话：场合规则放行，唯一挡下它的只能是取代关系。
        result = await _action(db, store).execute(_request(direct.id, 'direct'))

        assert '星野' not in result.text
        assert result.hit_count == 1
        after = db.execute(
            'SELECT strength, updated_at FROM memory_edges'
            ' WHERE source_id = ? AND target_id = ?',
            (low, high),
        ).fetchone()
        assert tuple(after) == tuple(before)

    async def test_superseded_counted_before_scope(self, db, monkeypatch):
        """两条判据同时成立时按取代归因，计数不重复也不漂移。"""

        store = MemoryStore(db)
        group, _direct = _streams(db)
        seed_id = _add_fact(db, store, '他喜欢手冲咖啡', ORIGIN_GROUP)
        stale_id = _add_fact(db, store, '他在星野公司上班', ORIGIN_DIRECT)
        fresh_id = _add_fact(db, store, '他现在在云岚公司上班', ORIGIN_DIRECT)
        db.execute(
            'UPDATE facts SET superseded_by = ? WHERE id = ?', (fresh_id, stale_id),
        )
        link_together(db, [('fact', seed_id), ('fact', stale_id)], NOW)

        emitted = []
        monkeypatch.setattr(
            'src.core.agent.cognition.trace.emit',
            lambda kind, **fields: emitted.append((kind, fields)),
        )
        # 群聊里这条旧值同时是 direct 来源，两条判据都成立。
        await _action(db, store).execute(_request(group.id, 'group'))

        assert [kind for kind, _ in emitted if kind == 'memory_fact_scope_blocked'] == []
        spread_fields = [f for kind, f in emitted if kind == 'memory_spread'][0]
        assert spread_fields['blockedSuperseded'] == 1
        assert spread_fields['spread'] == 0


class TestPendingRebuildEpisodeBlocked:
    """★S-8：用户纠正过的情节不该换条通道回来——屏蔽开关只堵直接召回而放过
    扩散，等于没堵。"""

    async def test_rebuild_episode_blocked_when_switch_on(self, db, monkeypatch):
        store = MemoryStore(db)
        group, _direct = _streams(db)
        seed_id = _add_fact(db, store, '他喜欢手冲咖啡', ORIGIN_GROUP)
        episode_id = _add_episode(store, group.id, '群里聊过周末烧烤')
        db.execute('UPDATE episodes SET needs_rebuild = 1 WHERE id = ?', (episode_id,))
        link_together(db, [('fact', seed_id), ('episode', episode_id)], NOW)

        emitted = []
        monkeypatch.setattr(
            'src.core.agent.cognition.trace.emit',
            lambda kind, **fields: emitted.append((kind, fields)),
        )
        action = _action(db, store, exclude_rebuild=True)
        result = await action.execute(_request(group.id, 'group'))

        assert '周末烧烤' not in result.text
        assert result.hit_count == 1
        spread_fields = [f for kind, f in emitted if kind == 'memory_spread'][0]
        assert spread_fields['blockedRebuild'] == 1
        # 它属于本 stream，被挡的理由不是会话隔离。
        assert spread_fields['blockedEpisodes'] == 0

    async def test_rebuild_episode_visible_when_switch_off(self, db):
        """开关关闭时保持原行为，与 recall_episodes 的同名参数同口径。"""

        store = MemoryStore(db)
        group, _direct = _streams(db)
        seed_id = _add_fact(db, store, '他喜欢手冲咖啡', ORIGIN_GROUP)
        episode_id = _add_episode(store, group.id, '群里聊过周末烧烤')
        db.execute('UPDATE episodes SET needs_rebuild = 1 WHERE id = ?', (episode_id,))
        link_together(db, [('fact', seed_id), ('episode', episode_id)], NOW)

        result = await _action(db, store).execute(_request(group.id, 'group'))

        assert '周末烧烤' in result.text
