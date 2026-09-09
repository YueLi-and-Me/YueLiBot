"""事实抽取一级的解析、归属、游标与触发口径。

覆盖 W1 规格里能用单测钉住的五条 ★ 断言：整批丢弃语义（★W1-4）、去重走 add_fact
（★W1-2）、群聊归属与未知编号丢弃（★W1-3）、抽取游标与摘要队列互不干扰（★W1-5），
以及触发阈值。★W1-1 是 24 小时真机观察项，不在此处；W9 的账本断言（slot 与 supersedes）见末尾各类。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Sequence

import pytest

from src.core.agent.fact_extract import (
    CURSOR_KEY,
    ExtractedFact,
    Participant,
    advance_cursor,
    Extraction,
    parse_extraction,
    persist_facts,
    persist_knowledge,
    read_cursor,
    render_dialogue,
    render_known_facts,
    run_extraction,
)
from src.core.memory.knowledge import search_knowledge
from src.core.memory.store import EpisodeInput, FactInput, MemoryStore
from src.core.platform_io.registry import StreamRegistry

STREAM_ID = 1
OWNER_PERSON_ID = 1
NOW = 1_800_000_000_000


class _StubProvider:
    """按预设文本回放一次模型流，并记录收到的请求。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: List[List[Dict[str, str]]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        self.requests.append(kwargs['messages'])
        yield {'text': self.text}


def _participants(store: MemoryStore, db: Any) -> Sequence[Participant]:
    registry = StreamRegistry(db)
    other = registry.create_person('contact', NOW)
    return (
        Participant(external_id='900000001', display_name='他', person_id=OWNER_PERSON_ID),
        Participant(external_id='2262378980', display_name='小明', person_id=other.id),
    )


def _fill(store: MemoryStore, count: int, person_id: int = OWNER_PERSON_ID) -> None:
    for i in range(count):
        store.append_message(STREAM_ID, person_id, 'user', f'第 {i} 条消息，聊了点别的东西', NOW + i)


class TestParseFacts:
    def test_empty_result_is_valid(self):
        """两个数组都空表示这批没什么可记的，是正常结果，不能与解析失败混为一谈。"""
        assert parse_extraction('{"facts": [], "knowledge": []}') == Extraction(facts=[], knowledge=[])

    def test_missing_keys_default_to_empty(self):
        """两个键都允许缺省：模型只写了事实时，知识按空处理而不是判废整批。"""
        got = parse_extraction('{"facts": []}')
        assert got == Extraction(facts=[], knowledge=[])

    def test_strips_code_fence(self):
        got = parse_extraction('```json\n{"facts":[{"person":"1","kind":"偏好","content":"他喜欢冰美式"}]}\n```')
        assert got.facts == [ExtractedFact(person_ref='1', kind='偏好', content='他喜欢冰美式')]

    def test_missing_kind_falls_back(self, monkeypatch):
        emitted = []
        monkeypatch.setattr(
            'src.core.agent.fact_extract.trace.emit',
            lambda event, **fields: emitted.append((event, fields)),
        )
        facts = parse_extraction('{"facts":[{"person":"1","content":"他在准备考研"}]}').facts
        assert facts is not None and facts[0].kind == '事件'
        assert emitted == [(
            'memory_fact_kind_normalized',
            {'rawKind': '<空>', 'normalizedKind': '事件'},
        )]

    def test_unknown_kind_is_normalized_and_traced(self, monkeypatch):
        """★C-3：枚举外类别不判废正文，但必须归一并留下 trace。"""

        emitted = []
        monkeypatch.setattr(
            'src.core.agent.fact_extract.trace.emit',
            lambda event, **fields: emitted.append((event, fields)),
        )

        got = parse_extraction(
            '{"facts":[{"person":"1","kind":"喜好","content":"他喜欢冰美式"}]}'
        )

        assert got.facts == [ExtractedFact('1', '事件', '他喜欢冰美式')]
        assert emitted == [(
            'memory_fact_kind_normalized',
            {'rawKind': '喜好', 'normalizedKind': '事件'},
        )]

    def test_retired_status_kind_is_normalized_and_traced(self, monkeypatch):
        """★K1-5：已退役的「状态」类归一到「事件」并留下 trace。

        旧提示词喂出来的存量抽取结果仍会带这个类别；归一让它落到默认曲线，
        而不是撞进枚举外分支之外的黑洞。
        """

        emitted = []
        monkeypatch.setattr(
            'src.core.agent.fact_extract.trace.emit',
            lambda event, **fields: emitted.append((event, fields)),
        )

        got = parse_extraction(
            '{"facts":[{"person":"1","kind":"状态","content":"他目前读大三"}]}'
        )

        assert got.facts == [ExtractedFact('1', '事件', '他目前读大三')]
        assert emitted == [(
            'memory_fact_kind_normalized',
            {'rawKind': '状态', 'normalizedKind': '事件'},
        )]

    def test_knowledge_is_extracted_alongside_facts(self):
        """知识候选与事实同一次往返产出，不为知识再读一遍同样的对话。"""
        got = parse_extraction(
            '{"facts":[{"person":"1","kind":"偏好","content":"他喜欢冰美式"}],'
            '"knowledge":["冰美式是意式浓缩加冰水"]}'
        )
        assert got.facts[0].content == '他喜欢冰美式'
        assert got.knowledge == ['冰美式是意式浓缩加冰水']

    def test_dirty_knowledge_entry_is_skipped_not_fatal(self):
        """知识是旁路产物：它的一条脏数据不该牵连本批事实的写入。"""
        got = parse_extraction('{"facts":[],"knowledge":["有效的一条", 123, "", null]}')
        assert got is not None and got.knowledge == ['有效的一条']

    @pytest.mark.parametrize('raw', [
        '抱歉，我没有找到值得记的内容',                       # 非 JSON
        '[{"person":"1","content":"x"}]',                      # 旧的裸数组契约，已废止
        '{"facts": "不是数组"}',                               # facts 类型错
        '{"facts":[{"kind":"喜好","content":"x"}]}',           # 事实缺归属
        '{"facts":[{"person":"1","content":"   "}]}',          # 事实正文空白
    ])
    def test_malformed_output_drops_whole_batch(self, raw):
        """★W1-4：模型没按契约输出时整批不可信，不做逐条挑拣。"""
        assert parse_extraction(raw) is None


@pytest.mark.asyncio
class TestPersistFacts:
    async def test_same_fact_reinforces_instead_of_duplicating(self, db):
        """★W1-2：去重完全由 add_fact 承担，重复写入不新增行。"""
        store = MemoryStore(db)
        people = _participants(store, db)
        fact = ExtractedFact(person_ref='900000001', kind='偏好', content='他喜欢喝冰美式')

        first = await persist_facts(store, [fact], people, db, NOW)
        second = await persist_facts(store, [fact], people, db, NOW + 1000)

        assert first == second
        assert store.fact_count(OWNER_PERSON_ID)['total'] == 1

    async def test_semantically_same_fact_merges(self, db):
        """措辞不同但语义相同的一条走 is_same_fact 合并，同样不新增行。"""
        store = MemoryStore(db)
        people = _participants(store, db)
        await persist_facts(store, [ExtractedFact('900000001', '偏好', '他喜欢喝冰美式咖啡')], people, db, NOW)
        await persist_facts(store, [ExtractedFact('900000001', '偏好', '他喜欢喝冰美式咖啡的')], people, db, NOW + 1)

        assert store.fact_count(OWNER_PERSON_ID)['total'] == 1

    async def test_third_person_fact_lands_on_that_person(self, db):
        """★W1-3：群聊里关于第三人的事实落到该人，不落到说话人。"""
        store = MemoryStore(db)
        people = _participants(store, db)
        other = people[1]

        await persist_facts(store, [ExtractedFact(other.external_id, '身份', '小明在读研二')], people, db, NOW)

        assert [f.content for f in store.top_facts(other.person_id, stream_kind='direct')] == ['小明在读研二']
        assert store.top_facts(OWNER_PERSON_ID, stream_kind='direct') == []

    async def test_unknown_person_ref_is_dropped(self, db):
        """★W1-3：编号不在名单里的整条丢弃，不猜、不挂到 owner 头上。"""
        store = MemoryStore(db)
        people = _participants(store, db)

        written = await persist_facts(
            store,
            [ExtractedFact('999999', '偏好', '某人喜欢什么')],
            people,
            db,
            NOW,
        )

        assert written == []
        assert store.fact_count(OWNER_PERSON_ID)['total'] == 0

    async def test_written_fact_gets_embedding_before_return(self, db):
        """★V-1：事实写入与向量持久化属于同一条后台链路。"""

        store = MemoryStore(db)
        people = _participants(store, db)
        fact = ExtractedFact('900000001', '偏好', '他喜欢手冲咖啡')
        calls = []

        async def embed_fact(fact_id: int, content: str) -> None:
            calls.append((fact_id, content))
            store.store_embedding(fact_id, b'\x00\x00\x80?')

        written = await persist_facts(
            store,
            [fact],
            people,
            db,
            NOW,
            embed_fact=embed_fact,
        )

        assert calls == [(written[0], fact.content)]
        row = db.execute('SELECT embedding FROM facts WHERE id = ?', (written[0],)).fetchone()
        assert row[0] is not None


@pytest.mark.asyncio
class TestPersistKnowledge:
    async def test_written_knowledge_calls_embedding_before_return(self, db):
        """★K-1：知识写入与向量持久化属于同一条后台链路。"""

        calls = []

        async def embed_knowledge(knowledge_id: int, content: str) -> None:
            calls.append((knowledge_id, content))

        written = await persist_knowledge(
            db,
            ['月球没有全球性磁场'],
            NOW,
            embed_knowledge=embed_knowledge,
        )

        assert calls == [(written[0], '月球没有全球性磁场')]

    async def test_duplicate_candidates_embed_only_once(self, db):
        """同一批去重命中同一知识行时不重复支付向量调用。"""

        calls = []

        async def embed_knowledge(knowledge_id: int, content: str) -> None:
            calls.append((knowledge_id, content))

        written = await persist_knowledge(
            db,
            ['月球没有全球性磁场', '  月球没有全球性磁场  '],
            NOW,
            embed_knowledge=embed_knowledge,
        )

        assert len(written) == 1
        assert calls == [(written[0], '月球没有全球性磁场')]


class TestCursor:
    def test_cursor_is_per_stream_and_persists(self, db):
        store = MemoryStore(db)
        assert read_cursor(store, STREAM_ID) == 0

        advance_cursor(store, STREAM_ID, 42)

        assert read_cursor(store, STREAM_ID) == 42
        assert read_cursor(store, 2) == 0
        assert store.read_json(CURSOR_KEY, {}) == {'1': 42}

    def test_summarized_messages_stay_visible_to_extraction(self, db):
        """★W1-5：抽取按自己的游标取消息，不受摘要归档影响。

        摘要消费消息的方式是给它们打上 episode_id；若抽取共用那个判据，
        两个消费者会互相吃掉输入且不报错。
        """
        store = MemoryStore(db)
        _fill(store, 6)
        batch = store.oldest_pending(STREAM_ID, 6)
        store.add_episode(
            STREAM_ID,
            EpisodeInput(
                summary='聊了点别的',
                cues=['别的'],
                started_at=NOW,
                ended_at=NOW + 6,
                message_ids=[m['id'] for m in batch],
            ),
            NOW,
        )

        assert store.pending_count(STREAM_ID) == 0
        assert len(store.messages_after(STREAM_ID, 0, 10)) == 6


class TestRunExtraction:
    @pytest.mark.asyncio
    async def test_below_threshold_does_not_call_model(self, db):
        store = MemoryStore(db)
        provider = _StubProvider('[]')
        _fill(store, 3)

        result = await run_extraction(
            store, provider, db, stream_id=STREAM_ID, stream_kind='direct', participants=_participants(store, db),
            bot_name='月璃', trigger_messages=8, batch_messages=4,
            temperature=0.1, max_tokens=256, now=NOW,
        )

        assert result is None and provider.requests == []

    @pytest.mark.asyncio
    async def test_known_facts_enter_the_prompt(self, db):
        """输入必须带上「已经记住的」清单——这是整块修法成立的关键。"""
        store = MemoryStore(db)
        people = _participants(store, db)
        store.add_fact(OWNER_PERSON_ID, FactInput(content='他喜欢喝冰美式', kind='喜好'), NOW)
        _fill(store, 10)
        provider = _StubProvider('[]')

        await run_extraction(
            store, provider, db, stream_id=STREAM_ID, stream_kind='direct', participants=people,
            bot_name='月璃', trigger_messages=8, batch_messages=4,
            temperature=0.1, max_tokens=256, now=NOW,
        )

        user_message = provider.requests[0][1]['content']
        assert '他喜欢喝冰美式' in user_message
        assert '[900000001]' in user_message

    @pytest.mark.asyncio
    async def test_success_advances_cursor(self, db):
        store = MemoryStore(db)
        people = _participants(store, db)
        _fill(store, 10)
        provider = _StubProvider(
            '{"facts":[{"person":"900000001","kind":"偏好","content":"他喜欢喝冰美式"}],'
            '"knowledge":["冰美式是意式浓缩加冰水"]}'
        )
        knowledge_calls = []

        async def embed_knowledge(knowledge_id: int, content: str) -> None:
            knowledge_calls.append((knowledge_id, content))

        written = await run_extraction(
            store, provider, db, stream_id=STREAM_ID, stream_kind='direct', participants=people,
            bot_name='月璃', trigger_messages=8, batch_messages=4,
            temperature=0.1, max_tokens=256, now=NOW,
            embed_knowledge=embed_knowledge,
        )

        assert written and store.fact_count(OWNER_PERSON_ID)['total'] == 1
        assert read_cursor(store, STREAM_ID) == 4
        # 知识候选与事实同一次往返写出，且写入即可检索（FTS 当场建好，不等离线重算）。
        assert [r[0] for r in db.execute('SELECT content FROM knowledge')] == ['冰美式是意式浓缩加冰水']
        assert [hit.content for hit in search_knowledge(db, '冰美式', 3)] == ['冰美式是意式浓缩加冰水']
        assert knowledge_calls == [(
            db.execute('SELECT id FROM knowledge').fetchone()[0],
            '冰美式是意式浓缩加冰水',
        )]

    @pytest.mark.asyncio
    async def test_malformed_output_keeps_cursor(self, db):
        """★W1-4：整批丢弃时游标不动，下次重跑同一批。"""
        store = MemoryStore(db)
        people = _participants(store, db)
        _fill(store, 10)
        provider = _StubProvider('我觉得没什么好记的')

        result = await run_extraction(
            store, provider, db, stream_id=STREAM_ID, stream_kind='direct', participants=people,
            bot_name='月璃', trigger_messages=8, batch_messages=4,
            temperature=0.1, max_tokens=256, now=NOW,
        )

        assert result is None
        assert read_cursor(store, STREAM_ID) == 0
        assert store.fact_count(OWNER_PERSON_ID)['total'] == 0

    @pytest.mark.asyncio
    async def test_model_failure_does_not_propagate(self, db):
        """抽取是旁路设施：模型故障不该让已完成的回合受任何影响。"""
        class _Failing:
            async def stream(self, **kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
                raise RuntimeError('上游炸了')
                yield {}

        store = MemoryStore(db)
        people = _participants(store, db)
        _fill(store, 10)

        result = await run_extraction(
            store, _Failing(), db, stream_id=STREAM_ID, stream_kind='direct', participants=people,
            bot_name='月璃', trigger_messages=8, batch_messages=4,
            temperature=0.1, max_tokens=256, now=NOW,
        )

        assert result is None and read_cursor(store, STREAM_ID) == 0


class TestRendering:
    def test_assistant_protocol_tags_are_stripped(self, db):
        store = MemoryStore(db)
        people = _participants(store, db)
        store.append_message(STREAM_ID, None, 'assistant', '<say>好啊</say><memory type="喜好">他喜欢咖啡</memory>', NOW)
        messages = store.messages_after(STREAM_ID, 0, 10)

        rendered = render_dialogue(messages, people, '月璃')

        assert rendered == '月璃：好啊'

    def test_multi_bubble_reply_stays_on_one_line(self, db):
        """她的多气泡回复压成一行，避免出现没有说话人前缀的续行。"""
        store = MemoryStore(db)
        people = _participants(store, db)
        store.append_message(STREAM_ID, None, 'assistant', '<say>好啊</say><say>那就明天</say>', NOW)
        messages = store.messages_after(STREAM_ID, 0, 10)

        rendered = render_dialogue(messages, people, '月璃')

        assert rendered.splitlines() == ['月璃：好啊 那就明天']

    def test_known_facts_are_capped_per_person(self, db):
        store = MemoryStore(db)
        people = _participants(store, db)
        for i in range(10):
            store.add_fact(OWNER_PERSON_ID, FactInput(content=f'他有第 {i} 个不同的习惯', kind='习惯'), NOW)

        rendered, _shown_ids = render_known_facts(store, people, NOW)

        assert 0 < len(rendered.splitlines()) <= 6


class TestParseLedgerFields:
    def test_slot_and_supersedes_are_parsed(self):
        got = parse_extraction(
            '{"facts":[{"person":"1","kind":"身份","content":"他现在住在杭州",'
            '"slot":"居住地","supersedes":"#12"}]}',
            known_fact_ids={12},
        )
        assert got is not None
        assert got.facts == [
            ExtractedFact('1', '身份', '他现在住在杭州', slot='居住地', supersedes=12)
        ]

    def test_supersedes_accepts_bare_int(self):
        got = parse_extraction(
            '{"facts":[{"person":"1","content":"他现在住在杭州","supersedes":12}]}',
            known_fact_ids={12},
        )
        assert got is not None and got.facts[0].supersedes == 12

    def test_l5_supersedes_outside_known_list_is_dropped_and_traced(self, monkeypatch):
        """★L-5：模型编造的清单外 ID 被丢弃并发 trace，取代关系不写库。"""

        emitted = []
        monkeypatch.setattr(
            'src.core.agent.fact_extract.trace.emit',
            lambda event, **fields: emitted.append((event, fields)),
        )

        got = parse_extraction(
            '{"facts":[{"person":"1","kind":"身份","content":"他现在住在杭州","supersedes":"#999"}]}',
            known_fact_ids={12},
        )

        assert got is not None and got.facts[0].supersedes == 0
        assert emitted == [(
            'memory_fact_supersede_dropped',
            {'supersededId': 999, 'reason': 'not_in_known_list'},
        )]

    def test_unparsable_supersedes_is_dropped(self):
        got = parse_extraction(
            '{"facts":[{"person":"1","content":"他现在住在杭州","supersedes":"不是编号"}]}',
            known_fact_ids={1},
        )
        assert got is not None and got.facts[0].supersedes == 0

    def test_out_of_range_slot_is_dropped(self):
        """槽位名是 2～6 字名词的约定；长度离谱按未提供处理，不污染冲突分组。"""

        got = parse_extraction(
            '{"facts":[{"person":"1","content":"他喜欢喝美式","slot":"他喜欢喝的东西"}]}'
        )
        assert got is not None and got.facts[0].slot == ''

    def test_facts_without_ledger_fields_default_to_empty(self):
        """两个字段都是可选的：模型不写时按空槽位、不取代处理。"""

        got = parse_extraction('{"facts":[{"person":"1","content":"他喜欢喝美式"}]}')
        assert got is not None
        assert got.facts[0].slot == '' and got.facts[0].supersedes == 0


@pytest.mark.asyncio
class TestPersistLedger:
    async def test_render_known_facts_carries_attribution_and_ids(self, db):
        """清单带归属与编号——模型据此分清是谁的，也能表达「取代 #123」。"""

        store = MemoryStore(db)
        people = _participants(store, db)
        fid = store.add_fact(
            OWNER_PERSON_ID, FactInput(content='他喜欢喝冰美式', kind='偏好'), NOW
        ).fact_id

        rendered, shown_ids = render_known_facts(store, people, NOW)

        assert rendered == f'  [900000001] #{fid} 他喜欢喝冰美式'
        assert shown_ids == frozenset({fid})

    async def test_supersede_is_written_and_traced(self, db, monkeypatch):
        """显式取代：新行落库、旧行回填 superseded_by，事件里看得见。"""

        store = MemoryStore(db)
        people = _participants(store, db)
        old = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        ).fact_id
        emitted = []
        monkeypatch.setattr(
            'src.core.agent.fact_extract.trace.emit',
            lambda event, **fields: emitted.append((event, fields)),
        )

        written = await persist_facts(
            store,
            [ExtractedFact('900000001', '身份', '他现在住在杭州', slot='居住地', supersedes=old)],
            people,
            db,
            NOW + 1,
        )

        assert written
        row = db.execute('SELECT superseded_by FROM facts WHERE id = ?', (old,)).fetchone()
        assert row[0] == written[0]
        assert any(
            event == 'memory_fact_superseded'
            and fields['factId'] == written[0]
            and fields['supersededId'] == old
            for event, fields in emitted
        )

    async def test_conflict_is_kept_and_traced(self, db, monkeypatch):
        """同槽异值未声明取代：两条都留着，冲突事件带槽位与对端 ID。"""

        store = MemoryStore(db)
        people = _participants(store, db)
        first = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        ).fact_id
        emitted = []
        monkeypatch.setattr(
            'src.core.agent.fact_extract.trace.emit',
            lambda event, **fields: emitted.append((event, fields)),
        )

        written = await persist_facts(
            store,
            [ExtractedFact('900000001', '身份', '他现在住在杭州', slot='居住地')],
            people,
            db,
            NOW + 1,
        )

        assert store.fact_count(OWNER_PERSON_ID)['total'] == 2
        assert any(
            event == 'memory_fact_conflict'
            and fields['factId'] == written[0]
            and fields['slot'] == '居住地'
            and fields['conflictWith'] == [first]
            for event, fields in emitted
        )

    async def test_run_extraction_applies_supersede_end_to_end(self, db):
        """端到端：模型引用清单里的 #ID 取代旧事实，旧行当场退出召回。"""

        store = MemoryStore(db)
        people = _participants(store, db)
        old = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content='他现在住在成都', kind='身份', slot='居住地'),
            NOW,
        ).fact_id
        _fill(store, 10)
        provider = _StubProvider(
            '{"facts":[{"person":"900000001","kind":"身份","content":"他现在住在杭州",'
            '"slot":"居住地","supersedes":"#' + str(old) + '"}]}'
        )

        written = await run_extraction(
            store, provider, db, stream_id=STREAM_ID, stream_kind='direct', participants=people,
            bot_name='月璃', trigger_messages=8, batch_messages=4,
            temperature=0.1, max_tokens=256, now=NOW,
        )

        assert written
        row = db.execute('SELECT superseded_by FROM facts WHERE id = ?', (old,)).fetchone()
        assert row[0] == written[0]
        assert [fact.id for fact in store.top_facts(OWNER_PERSON_ID, 5, NOW, stream_kind='direct')] == written
