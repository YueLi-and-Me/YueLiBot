"""事件账本、实时事件和来源字段。"""

from __future__ import annotations

import asyncio
import json

from src.core.config.schema import Config
from src.core.memory.store import ScopedFact
from src.core.observe import events
from src.core.observe.store import event_store
from src.core.services.chat import ChatService, InboundMessage, _RetrievalTrace
from src.core.services.proactive import AwarenessService
from src.core.schedule.timeline import ActivityTimeline
from src.desktop.sensor import DesktopSensor


class _FakeProvider:
    model = 'fake-model'

    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks

    async def stream(self, messages, temperature=0.85, max_tokens=None, signal=None):
        for chunk in self._chunks:
            yield chunk


class _FailingProvider:
    model = 'fake-model'

    async def stream(self, messages, temperature=0.85, max_tokens=None, signal=None):
        raise RuntimeError('模拟网络故障')
        yield


async def _noop_push(channel, payload, stream_id=1):
    return None


async def _send_and_wait(chat: ChatService, text: str) -> int:
    context = chat.desktop_context
    await chat.send(InboundMessage(text=text, context=context))
    await chat._tick()
    turn = chat._active_turns[context.stream.id]
    inflight = chat._inflight.get(context.stream.id)
    if inflight is not None:
        await asyncio.wait_for(inflight.task, timeout=5)
    return turn


def _clear() -> None:
    event_store.clear()
    events.reset_for_tests()


def _stored() -> list[dict]:
    return event_store.since(0).events


def test_event_seq_and_since_filters() -> None:
    _clear()
    first = events.emit('a', x=1)
    second = events.emit('b', x=2)
    assert first['seq'] < second['seq']
    assert event_store.since(first['seq']).events == [second]
    assert event_store.since(second['seq']).events == []


async def test_foreground_is_live_only_and_never_contains_title(db) -> None:
    _clear()
    subscriber = events.broadcaster.subscribe()
    chat = ChatService(db, None, None, None, _noop_push, cfg=Config())
    cfg = Config()
    service = AwarenessService(
        chat=chat,
        schedule=None,
        timeline=ActivityTimeline(db),
        cfg=cfg,
        push_event=_noop_push,
        sensor=DesktopSensor(cfg, _noop_push),
    )

    sensitive = '工资表_2026Q3_客户名单.xlsx'
    service.on_foreground({'process': 'EXCEL.EXE', 'title': sensitive, 'fullscreen': False})
    await asyncio.sleep(0.02)

    live_entries = []
    while not subscriber.queue.empty():
        live_entries.append(subscriber.queue.get_nowait())
    dumped = json.dumps(live_entries, ensure_ascii=False)
    assert '工资' not in dumped
    assert '客户名单' not in dumped
    assert any(entry['kind'] == 'foreground' for entry in live_entries)
    assert all(entry['kind'] != 'foreground' for entry in _stored())
    events.broadcaster.unsubscribe(subscriber)


async def test_chat_send_persists_trace_and_broadcasts_chunks(db) -> None:
    _clear()
    subscriber = events.broadcaster.subscribe()
    provider = _FakeProvider([
        {'text': '你好'},
        {'text': '呀', 'reasoning': '想了想要不要打招呼'},
    ])
    chat = ChatService(db, provider, provider, provider, _noop_push, cfg=Config())

    turn = await _send_and_wait(chat, '在干嘛')
    await asyncio.sleep(0)
    stored = _stored()
    kinds = [entry['kind'] for entry in stored]
    assert {'user_input', 'memory_retrieval_trace', 'llm_request', 'llm_final'} <= set(kinds)
    assert 'llm_chunk' not in kinds

    live_entries = []
    while not subscriber.queue.empty():
        live_entries.append(subscriber.queue.get_nowait())
    chunks = [entry for entry in live_entries if entry['kind'] == 'llm_chunk']
    assert len(chunks) == 2
    assert chunks[1]['reasoning'] == '想了想要不要打招呼'

    user_entry = next(entry for entry in stored if entry['kind'] == 'user_input')
    request_entry = next(entry for entry in stored if entry['kind'] == 'llm_request')
    final_entry = next(entry for entry in stored if entry['kind'] == 'llm_final')
    retrieval_entry = next(
        entry for entry in stored if entry['kind'] == 'memory_retrieval_trace'
    )
    assert user_entry['turnId'] == turn
    assert request_entry['turnId'] == turn
    assert final_entry['turnId'] == turn
    assert retrieval_entry['turnId'] == turn
    assert retrieval_entry['currentText'] == '在干嘛'
    assert retrieval_entry['conversationImpression'] == ''
    assert retrieval_entry['candidatePool'] == []
    assert retrieval_entry['promptFactIds'] == []
    assert user_entry['text'] == '在干嘛'
    assert isinstance(request_entry['messages'], list) and request_entry['messages']
    assert final_entry['text'] == '你好呀'
    for entry in (user_entry, request_entry, final_entry):
        assert entry['streamId'] == 1
        assert entry['platform'] == 'desktop'
        assert entry['personId'] == 1
        assert entry['personKind'] == 'owner'
        assert entry['streamKind'] == 'desktop'
        assert entry['sourceLabel'] == '桌面'
    events.broadcaster.unsubscribe(subscriber)


async def test_chat_retrieval_trace_records_both_queries_union_and_prompt_ids(
    db,
    monkeypatch,
) -> None:
    """生产回合留痕应还原双检索词、去重候选池和实际提示词选择。"""

    _clear()
    provider = _FakeProvider([{'text': '接着说呀'}])
    chat = ChatService(db, provider, provider, provider, _noop_push, cfg=Config())
    impression = '群里先前在讨论下周一起去杭州。'
    queries = []

    async def current_impression(_context, _now):
        return impression

    def recall(_person_ids, query, _limit, _now, **kwargs):
        queries.append((query, kwargs))
        if query == '嗯嗯，那后来呢':
            return [
                ScopedFact(12, 'event', '约好下周去杭州', 0.9, 0.875, person_id=1),
                ScopedFact(9, 'preference', '喜欢西湖', 0.8, 0.625, person_id=1),
            ]
        if query == impression:
            return [
                ScopedFact(9, 'preference', '喜欢西湖', 0.8, 0.95, person_id=1),
                ScopedFact(7, 'event', '准备查看天气', 0.7, 0.5, person_id=1),
            ]
        raise AssertionError(f'收到意外检索词：{query}')

    monkeypatch.setattr(chat, '_conversation_impression', current_impression)
    monkeypatch.setattr(chat.memory, 'recall_facts_in_scope', recall)

    turn = await _send_and_wait(chat, '嗯嗯，那后来呢')

    entries = event_store.search(
        turn_id=turn,
        kinds=['memory_retrieval_trace'],
    ).events
    assert len(entries) == 1
    assert [query for query, _ in queries] == ['嗯嗯，那后来呢', impression]
    assert all(options['return_candidates'] is True for _, options in queries)
    assert entries[0]['currentText'] == '嗯嗯，那后来呢'
    assert entries[0]['conversationImpression'] == impression
    assert entries[0]['candidatePool'] == [
        {'factId': 12, 'score': 0.875},
        {'factId': 9, 'score': 0.625},
        {'factId': 7, 'score': 0.5},
    ]
    assert entries[0]['promptFactIds'] == [12, 9, 7]


def test_retrieval_trace_keeps_full_queries_candidates_and_emits_once() -> None:
    """重放字段必须无损；同一上下文被决策与回复重复渲染时只落一条事件。"""

    _clear()
    impression = '一段需要完整保留的会话印象。' * 300
    retrieval = _RetrievalTrace(
        turn_id=77,
        stream_id=3,
        current_text='嗯嗯，那后来呢',
        conversation_impression=impression,
        candidate_pool=((12, 0.875), (9, 0.625)),
    )

    retrieval.emit_once([12, 9])
    retrieval.emit_once([9])

    entries = event_store.search(
        turn_id=77,
        kinds=['memory_retrieval_trace'],
    ).events
    assert len(entries) == 1
    assert entries[0]['streamId'] == 3
    assert entries[0]['currentText'] == '嗯嗯，那后来呢'
    assert entries[0]['conversationImpression'] == impression
    assert entries[0]['currentTextChars'] == len('嗯嗯，那后来呢')
    assert entries[0]['impressionChars'] == len(impression)
    assert entries[0]['candidateCount'] == 2
    assert entries[0]['candidatePool'] == [
        {'factId': 12, 'score': 0.875},
        {'factId': 9, 'score': 0.625},
    ]
    assert entries[0]['promptFactIds'] == [12, 9]


async def test_excluded_kinds_are_dropped_before_enqueue() -> None:
    """屏蔽类型不占用订阅者队列容量，避免高频 llm_chunk 灌满队列触发溢出。"""

    _clear()
    ledger = events.broadcaster.subscribe(exclude_kinds=events.LIVE_ONLY_KINDS)
    mirror = events.broadcaster.subscribe()

    events.emit('llm_chunk', turnId=1, text='片段')
    events.emit('user_input', turnId=1, text='正文')
    await asyncio.sleep(0)

    ledger_kinds = []
    while not ledger.queue.empty():
        ledger_kinds.append(ledger.queue.get_nowait()['kind'])
    mirror_kinds = []
    while not mirror.queue.empty():
        mirror_kinds.append(mirror.queue.get_nowait()['kind'])

    # 事件账本订阅者只应收到持久化事件；未加过滤的镜像订阅者仍收到全部广播。
    assert ledger_kinds == ['user_input']
    assert 'llm_chunk' in mirror_kinds and 'user_input' in mirror_kinds
    assert not ledger.overflowed.is_set()
    events.broadcaster.unsubscribe(ledger)
    events.broadcaster.unsubscribe(mirror)


async def test_memory_tag_produces_no_memory_fact_event(db) -> None:
    """<memory> 双保险已拆除：标签被吞掉，事件账本不再出现 memory_fact。

    原断言（标签产出 memory_fact 事件）随 <memory> 写入路径一并删除，
    本用例镜像 test_parser 的吞没契约，防止这条路径复活。
    """
    _clear()
    provider = _FakeProvider([{'text': '<memory type="偏好">玩家喜欢深夜写代码</memory>好的'}])
    chat = ChatService(db, provider, provider, provider, _noop_push, cfg=Config())
    await _send_and_wait(chat, '记一下')

    assert all(entry['kind'] != 'memory_fact' for entry in _stored())


async def test_llm_error_is_persisted(db) -> None:
    _clear()
    provider = _FailingProvider()
    chat = ChatService(db, provider, provider, provider, _noop_push, cfg=Config())
    turn = await _send_and_wait(chat, '在吗')

    error = next(entry for entry in _stored() if entry['kind'] == 'llm_error')
    assert error['turnId'] == turn
    assert error['errorKind'] == 'unknown'
    assert '模拟网络故障' in error['message']


async def test_event_content_is_verbatim(db) -> None:
    _clear()
    provider = _FakeProvider([{'text': '你好'}, {'text': '呀'}])
    chat = ChatService(db, provider, provider, provider, _noop_push, cfg=Config())
    await _send_and_wait(chat, '这句话要原样出现在事件账本里')

    stored = _stored()
    user_entry = next(entry for entry in stored if entry['kind'] == 'user_input')
    request_entry = next(entry for entry in stored if entry['kind'] == 'llm_request')
    assert user_entry['text'] == '这句话要原样出现在事件账本里'
    assert any(message.get('role') == 'system' for message in request_entry['messages'])


def test_origin_fields_ride_along_on_every_kind() -> None:
    _clear()
    events.bind_origin(
        stream_id=2,
        platform='qq',
        person_id=2,
        person_kind='contact',
        sender_external_id='10001',
        sender_nickname='真实昵称',
        sender_group_card='群名片',
        sender_display_name='群名片',
        sender_label='群名片（QQ昵称：真实昵称 · QQ号：10001）',
        stream_kind='group',
        stream_external_id='629201002',
        source_label='群聊·629201002',
        bot_name='月璃',
    )
    entry = events.emit('llm_request', turnId=7)

    assert entry['streamId'] == 2
    assert entry['platform'] == 'qq'
    assert entry['personId'] == 2
    assert entry['personKind'] == 'contact'
    assert entry['senderExternalId'] == '10001'
    assert entry['streamKind'] == 'group'
    assert entry['streamExternalId'] == '629201002'
    assert entry['sourceLabel'] == '群聊·629201002'
    assert entry['turnId'] == 7


def test_explicit_fields_win_over_bound_origin() -> None:
    _clear()
    events.bind_origin(
        stream_id=1,
        platform='desktop',
        person_id=1,
        person_kind='owner',
        sender_external_id='',
        sender_nickname='',
        sender_group_card='',
        sender_display_name='你',
        sender_label='你',
        stream_kind='desktop',
        stream_external_id='desktop',
        source_label='桌面',
        bot_name='月璃',
    )
    entry = events.emit('outbound_delivered', platform='qq', streamId=2)
    assert entry['platform'] == 'qq'
    assert entry['streamId'] == 2


def test_scope_blocked_identifies_fact_origin_and_target_stream() -> None:
    """W7 的可见性事件同时说清事实来自私聊、又在哪个群被挡下。"""

    _clear()
    events.bind_origin(
        stream_id=3,
        platform='qq',
        person_id=1,
        person_kind='owner',
        sender_external_id='10001',
        sender_nickname='昵称',
        sender_group_card='群名片',
        sender_display_name='群名片',
        sender_label='群名片（QQ昵称：昵称 · QQ号：10001）',
        stream_kind='group',
        stream_external_id='629201002',
        source_label='群聊·629201002',
        bot_name='月璃',
    )

    entry = events.emit(
        'memory_fact_scope_blocked',
        turnId=8,
        streamKind='group',
        blocked=3,
    )

    assert entry['factOriginKind'] == 'direct'
    assert entry['streamId'] == 3
    assert entry['streamKind'] == 'group'
    assert entry['streamExternalId'] == '629201002'
    assert entry['sourceLabel'] == '群聊·629201002'

