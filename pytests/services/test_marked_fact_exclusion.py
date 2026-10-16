"""「已被纠正」的事实在读取入口排除，先于任何截断生效。

反馈纠错给事实打上标记后，读取侧此前只在注入提示词时才把它滤掉：截断在前、
过滤在后，被纠正的事实照样占掉名额，空位不补，回复路径还会给它加强度。
本文件钉住改到读取入口之后的行为：

1. 四个读取入口（在场者召回、跨人物召回、按留存度取前 N、槽位冲突分组）在
   ``exclude_marked`` 打开时排除带标记的事实，排除先于 ``LIMIT``；关闭时照常返回；
2. 决策提示词带满条数上限，留痕记录过滤后的候选池与开关取值；
3. 回复路径的强化写回碰不到被纠正的事实；
4. 主动消息先排除再取前 5 条；
5. 回想工具与联想扩散都不再带出被纠正的事实，扩散计入 ``blockedMarked``；
6. 检索评估按留痕的 ``excludeMarked`` 重放，正例只认候选池内的事实。
"""

from __future__ import annotations

from typing import Any

import json
import sqlite3

from src.core.agent.cognition import CognitiveRequest, RecallAction
from src.core.config.schema import Config, MemoryFeedbackConfig
from src.core.memory import tuning
from src.core.memory.association import link_together
from src.core.memory.scope import ORIGIN_DIRECT
from src.core.memory.store import FactInput, MemoryStore
from src.core.runtime.clock import now as current_time
from src.core.services.chat import ChatService, _facts_for_prompt

NOW = 1_800_000_000_000
OWNER_PERSON_ID = 1
QUERY = '咖啡'
# 七条互不相似的事实，都能被「咖啡」检索到；条数比默认的进提示词上限（6）多一条。
COFFEE_FACTS = (
    '他喜欢喝手冲咖啡',
    '他常去楼下的咖啡馆自习',
    '他收藏了一整套咖啡杯',
    '他学过一阵子咖啡拉花',
    '他早上只喝美式咖啡',
    '他买过一台意式咖啡机',
    '他讨厌速溶咖啡的味道',
)


async def _noop_push(_channel: str, _payload: object, _stream_id: int = 1) -> None:
    return None


def _chat(db: sqlite3.Connection, *, feedback: bool) -> ChatService:
    return ChatService(
        db=db,
        chat_provider=None,
        proactive_provider=None,
        summary_provider=None,
        push_event=_noop_push,
        cfg=Config(memory_feedback=MemoryFeedbackConfig(enabled=feedback)),
    )


def _add_facts(store: MemoryStore, contents: tuple[str, ...], now: int, **fields: Any) -> list[int]:
    ids = [
        store.add_fact(OWNER_PERSON_ID, FactInput(content=content, **fields), now).fact_id
        for content in contents
    ]
    assert len(set(ids)) == len(contents), '测试事实被去重合并了，换一组正文'
    return ids


def _mark(db: sqlite3.Connection, fact_id: int) -> None:
    db.execute(
        'INSERT INTO memory_feedback_results'
        ' (fact_id, person_id, stream_id, confidence, corrected_content,'
        '  new_fact_id, marked, created_at)'
        " VALUES (?, ?, 1, 0.95, '', 0, 1, ?)",
        (fact_id, OWNER_PERSON_ID, NOW),
    )
    db.commit()


def _in_scope(store: MemoryStore, limit: int, *, exclude_marked: bool) -> list[int]:
    return [
        fact.id for fact in store.recall_facts_in_scope(
            [OWNER_PERSON_ID], QUERY, limit, NOW,
            stream_kind='direct', return_candidates=True,
            exclude_marked=exclude_marked,
        )
    ]


class TestReadEntries:
    """读取入口在截断之前排除被纠正的事实。"""

    def test_in_scope_exclusion_precedes_fts_limit(self, db):
        store = MemoryStore(db)
        ids = _add_facts(store, COFFEE_FACTS[:4], NOW)
        # limit=1 时 FTS 只取 3 条候选，4 条匹配里总有 1 条落在截断之外。
        before = _in_scope(store, 1, exclude_marked=False)
        assert len(before) == 3
        _mark(db, before[0])

        after = _in_scope(store, 1, exclude_marked=True)

        # 排除若发生在截断之后只会剩 2 条；先排除，原本被截掉的那条才能补进来。
        assert set(after) == set(ids) - {before[0]}
        assert _in_scope(store, 1, exclude_marked=False) == before

    def test_across_persons_excludes_marked(self, db):
        store = MemoryStore(db)
        ids = _add_facts(store, COFFEE_FACTS[:2], NOW)
        _mark(db, ids[0])

        def recalled(exclude_marked: bool) -> set[int]:
            return {
                fact.id for fact in store.recall_facts_across_persons(
                    QUERY, 5, NOW, stream_kind='direct', exclude_marked=exclude_marked,
                )
            }

        assert recalled(True) == {ids[1]}
        assert recalled(False) == set(ids)

    def test_top_facts_excludes_before_limit(self, db):
        store = MemoryStore(db)
        # 写入时刻越晚留存度越高：最新那条排第一，标记它之后前 2 条应顺延。
        ids = [
            store.add_fact(
                OWNER_PERSON_ID, FactInput(content=content), NOW - offset * 3_600_000,
            ).fact_id
            for offset, content in enumerate(COFFEE_FACTS[:3])
        ]
        _mark(db, ids[0])

        def top(exclude_marked: bool) -> list[int]:
            return [
                fact.id for fact in store.top_facts(
                    OWNER_PERSON_ID, 2, NOW, stream_kind='direct',
                    exclude_marked=exclude_marked,
                )
            ]

        assert top(True) == [ids[1], ids[2]]
        assert top(False) == [ids[0], ids[1]]

    def test_slot_conflicts_ignore_marked_member(self, db):
        store = MemoryStore(db)
        old_id = store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他住在深圳宝安区', slot='居住地'), NOW,
        ).fact_id
        new_id = store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他搬到了杭州西湖区', slot='居住地'), NOW,
        ).fact_id
        _mark(db, old_id)

        assert store.slot_conflicts(OWNER_PERSON_ID, [new_id], exclude_marked=True) == {}
        assert set(store.slot_conflicts(OWNER_PERSON_ID, [new_id])) == {new_id}
        # 注入侧：对手被纠正后剩下那条按普通事实呈现，也不把被纠正的补回来。
        recalled = store.recall_facts_in_scope(
            [OWNER_PERSON_ID], '杭州', 5, NOW, stream_kind='direct', exclude_marked=True,
        )
        items = _facts_for_prompt(store, OWNER_PERSON_ID, recalled, exclude_marked=True)
        assert [(item.fact_id, item.conflicting) for item in items] == [(new_id, False)]


class TestTurnContext:
    """决策提示词、召回留痕与回复路径的强化写回。"""

    def _prepare(self, db, monkeypatch, *, feedback: bool):
        chat = _chat(db, feedback=feedback)
        limit = chat.fact_recall_limit
        ids = _add_facts(chat.memory, COFFEE_FACTS[:limit + 1], NOW)
        context = chat.desktop_context
        top = [
            fact.id for fact in chat.memory.recall_facts_in_scope(
                [context.person.id], QUERY, limit, NOW,
                stream_kind=context.stream.kind, return_candidates=True,
            )
        ]
        marked = top[0]
        _mark(db, marked)
        emitted: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            'src.core.services.chat.state.trace.emit',
            lambda kind, **fields: emitted.append((kind, fields)),
        )
        prepared = chat._prepare_turn_context(context, QUERY, NOW, turn_id=7)
        return chat, prepared, ids, marked, limit, emitted

    async def test_decision_prompt_fills_limit_and_trace_records_filtered_pool(
        self, db, monkeypatch,
    ):
        chat, prepared, ids, marked, limit, emitted = self._prepare(
            db, monkeypatch, feedback=True,
        )

        chat._render_prepared_context(prepared)

        trace = [fields for kind, fields in emitted if kind == 'memory_retrieval_trace'][0]
        assert trace['excludeMarked'] is True
        assert marked not in {item['factId'] for item in trace['candidatePool']}
        # 过滤在截断之后时只剩 limit - 1 条。
        assert len(trace['promptFactIds']) == limit
        assert set(trace['promptFactIds']) == set(ids) - {marked}

    async def test_feedback_disabled_keeps_marked_fact(self, db, monkeypatch):
        chat, prepared, _ids, marked, limit, emitted = self._prepare(
            db, monkeypatch, feedback=False,
        )

        chat._render_prepared_context(prepared)

        trace = [fields for kind, fields in emitted if kind == 'memory_retrieval_trace'][0]
        assert trace['excludeMarked'] is False
        assert marked in trace['promptFactIds']
        assert len(trace['promptFactIds']) == limit

    async def test_reply_path_never_reinforces_marked_fact(self, db, monkeypatch):
        chat, prepared, ids, marked, _limit, _emitted = self._prepare(
            db, monkeypatch, feedback=True,
        )

        def snapshot() -> dict[int, tuple[float, int]]:
            return {
                int(row[0]): (float(row[1]), int(row[2]))
                for row in db.execute('SELECT id, strength, hit_count FROM facts')
            }

        before = snapshot()
        await chat._enrich_prepared_context(prepared, None, {}, None)
        after = snapshot()

        assert after[marked] == before[marked]
        assert all(after[fact_id][1] == before[fact_id][1] + 1 for fact_id in ids if fact_id != marked)


class TestProactive:
    """主动消息先排除被纠正的事实，再取留存度最高的 5 条。"""

    async def test_proactive_prompt_fills_five_unmarked_facts(self, db):
        chat = _chat(db, feedback=True)
        now = current_time()
        # 写入时刻越晚留存度越高；被标记的是最新那条，它在旧顺序里一定进前 5。
        ids = [
            chat.memory.add_fact(
                OWNER_PERSON_ID, FactInput(content=content), now - offset * 3_600_000,
            ).fact_id
            for offset, content in enumerate(COFFEE_FACTS[:6])
        ]
        _mark(db, ids[0])
        captured: list[list[dict]] = []

        class _Provider:
            async def stream(self, messages, **_kwargs):
                captured.append(messages)
                yield {'text': '<say>在吗</say>'}

        chat._proactive_provider = _Provider()

        await chat.compose_proactive(chat.desktop_context, '正在写代码')

        system = captured[0][0]['content']
        assert COFFEE_FACTS[0] not in system
        assert all(content in system for content in COFFEE_FACTS[1:6])


def _recall_action(db, store: MemoryStore, *, exclude_marked: bool) -> RecallAction:
    return RecallAction(
        store, lambda person_id, stream_id: '凌白', db, exclude_marked=exclude_marked,
    )


def _recall_request(stream_id: int) -> CognitiveRequest:
    return CognitiveRequest(
        action='recall',
        query=QUERY,
        stream_id=stream_id,
        stream_kind='direct',
        person_ids=(OWNER_PERSON_ID,),
        message_watermark=0,
    )


class TestRecallTool:
    """回想工具的直接命中与联想扩散都不带出被纠正的事实。"""

    async def test_direct_hits_exclude_marked(self, db):
        store = MemoryStore(db)
        marked, kept = _add_facts(store, COFFEE_FACTS[:2], NOW, origin_kind=ORIGIN_DIRECT)
        _mark(db, marked)
        stream = store._db.execute(
            "INSERT INTO streams (platform, kind, external_id) VALUES ('qq', 'direct', 'mk-1')"
        ).lastrowid

        on = await _recall_action(db, store, exclude_marked=True).execute(_recall_request(stream))
        off = await _recall_action(db, store, exclude_marked=False).execute(_recall_request(stream))

        assert COFFEE_FACTS[0] not in on.text and COFFEE_FACTS[1] in on.text
        assert COFFEE_FACTS[0] in off.text

    async def test_spread_blocks_marked_fact_and_counts_it(self, db, monkeypatch):
        # 回想默认不扩散，这里测的是联想打开时的过滤。
        monkeypatch.setattr(tuning, '_active_overrides', {'ppr_hops': 2})
        store = MemoryStore(db)
        (seed,) = _add_facts(store, COFFEE_FACTS[:1], NOW, origin_kind=ORIGIN_DIRECT)
        (linked,) = _add_facts(store, ('他周末在星野公司加班',), NOW, origin_kind=ORIGIN_DIRECT)
        _mark(db, linked)
        link_together(db, [('fact', seed), ('fact', linked)], NOW)
        stream = store._db.execute(
            "INSERT INTO streams (platform, kind, external_id) VALUES ('qq', 'direct', 'mk-2')"
        ).lastrowid
        emitted: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            'src.core.agent.cognition.trace.emit',
            lambda kind, **fields: emitted.append((kind, fields)),
        )

        on = await _recall_action(db, store, exclude_marked=True).execute(_recall_request(stream))

        assert '星野' not in on.text
        spread = [fields for kind, fields in emitted if kind == 'memory_spread'][0]
        assert spread['blockedMarked'] == 1
        assert spread['blockedSuperseded'] == 0
        off = await _recall_action(db, store, exclude_marked=False).execute(_recall_request(stream))
        assert '星野' in off.text


def _seed_trace(
    db: sqlite3.Connection,
    *,
    turn_id: int,
    pool: list[tuple[int, float]],
    prompt_fact_ids: list[int],
    exclude_marked: bool | None,
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO streams (id, platform, kind, external_id)"
        " VALUES (1, 'qq', 'direct', 'mk-eval')"
    )
    db.execute(
        'INSERT INTO messages (role, content, created_at, stream_id, sender_person_id)'
        " VALUES ('user', ?, ?, 1, ?)",
        (QUERY, NOW - 1, OWNER_PERSON_ID),
    )
    payload: dict[str, Any] = {
        'currentText': QUERY,
        'conversationImpression': '',
        'candidatePool': [{'factId': fact_id, 'score': score} for fact_id, score in pool],
        'promptFactIds': prompt_fact_ids,
    }
    # None 表示改动之前的旧留痕，没有这个字段。
    if exclude_marked is not None:
        payload['excludeMarked'] = exclude_marked
    for kind, body in (('memory_retrieval_trace', payload), ('outbound_delivered', {'text': 'ok'})):
        db.execute(
            'INSERT INTO pipeline_events (at, stream_id, turn_id, stage, kind, payload)'
            " VALUES (?, 1, ?, '', ?, ?)",
            (NOW, turn_id, kind, json.dumps(body, ensure_ascii=False)),
        )
    db.commit()


class TestRetrievalEvaluation:
    """评估按留痕记录的开关重放，正例只认候选池内的事实。"""

    def _pool(self, store: MemoryStore, *, exclude_marked: bool) -> list[tuple[int, float]]:
        return [
            (fact.id, fact.score) for fact in tuning.replay_union_recall(
                store, [OWNER_PERSON_ID], QUERY, '', 2, NOW,
                stream_kind='direct', exclude_marked=exclude_marked,
            )
        ]

    async def test_new_trace_replays_filtered_pool_and_scores_one(self, db):
        store = MemoryStore(db)
        _add_facts(store, COFFEE_FACTS[:4], NOW)
        marked = self._pool(store, exclude_marked=False)[0][0]
        _mark(db, marked)
        pool = self._pool(store, exclude_marked=True)
        _seed_trace(
            db, turn_id=1, pool=pool,
            prompt_fact_ids=[fact_id for fact_id, _ in pool[:2]], exclude_marked=True,
        )

        report = await tuning.evaluate_round2(
            store, tuning.build_trace_samples(db), {'fact_recall_limit': 2},
        )

        row = report.per_turn[0]
        assert row['reconcile']['status'] == 'match'
        assert marked not in row['ranked_ids']
        assert row['ndcg'] == 1.0

    async def test_legacy_trace_replays_unfiltered_pool(self, db):
        store = MemoryStore(db)
        _add_facts(store, COFFEE_FACTS[:4], NOW)
        legacy_pool = self._pool(store, exclude_marked=False)
        marked = legacy_pool[0][0]
        _mark(db, marked)
        # 旧版生产在注入时才滤掉被纠正的事实：池里有它，提示词里没有。
        _seed_trace(
            db, turn_id=1, pool=legacy_pool,
            prompt_fact_ids=[legacy_pool[1][0]], exclude_marked=None,
        )

        samples = tuning.build_trace_samples(db)
        report = await tuning.evaluate_round2(store, samples, {'fact_recall_limit': 2})

        assert samples[0].exclude_marked is False
        assert report.per_turn[0]['reconcile']['status'] == 'match'

    def test_prompt_fact_outside_pool_is_not_positive(self, db):
        store = MemoryStore(db)
        in_pool, appended = _add_facts(store, COFFEE_FACTS[:2], NOW)
        # 冲突组补齐的成员进了提示词但不在候选池里。
        _seed_trace(
            db, turn_id=1, pool=[(in_pool, 0.5)],
            prompt_fact_ids=[in_pool, appended], exclude_marked=True,
        )
        _seed_trace(
            db, turn_id=2, pool=[(in_pool, 0.5)],
            prompt_fact_ids=[appended], exclude_marked=True,
        )

        first, second = tuning.build_trace_samples(db)

        assert first.positive_ids == [in_pool]
        assert first.scorable
        assert second.positive_ids == []
        assert not second.scorable
