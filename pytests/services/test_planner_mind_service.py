"""规划器心理活动的跨回合保留、跟进窗口与「对谁说」附注的服务层回归。

覆盖：
- 心理活动只在终局动作生效后写入 meta 表：silent / reply 写，模型失败、shadow、
  被作废的回合不写；
- 心理活动行按时间插进决策层历史，写正文那一环看不到这些行、只拿这一轮的想法；
- 心理活动不进 messages 表，也不进事实抽取的输入；
- 跟进窗口只对她最近一次生效回合回应过的人放行，speak 之后对全场放行，进程里
  还没有生效回合时为假；入口门控与批次门控读同一份事实；
- 可选清单按入站事实标出「这条在对谁说」，回应过的消息带上提醒。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import json
import re

from src.core.agent.fact_extract import render_dialogue
from src.core.api.http import PlatformInboundBody, platform_inbound
from src.core.api.state import app_state
from src.core.config.schema import Config, GroupChatConfig
from src.core.observe.store import event_store
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import DeliveryReceipt, InboundMessage, OutboundMessage
from src.core.runtime.clock import now as current_time
from src.core.services.chat import ChatService
from src.core.services.chat.agent_protocol import RESPONDED_NOTE
from src.core.services.chat.mind import MindEntry, mind_key
from src.core.services.chat.state import _BufferedMessage

from pytests.services.test_reply_superseded_by_follow_up import (
    _InjectingProvider,
    _RecordingBroker,
    _chat as _xml_chat,
    _mention,
    _reply,
    _resolve,
    _run_turn,
    _text,
)


class _Broker:
    """记录并成功回执的出站路由替身。"""

    def __init__(self) -> None:
        self.dispatched: List[OutboundMessage] = []

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        self.dispatched.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=['out-1'],
        )


class _Planner:
    """按调用次序产出脚本里的响应。

    脚本项是字典时原样产出；是可调用对象时以本次下发的工具声明调用它，用于
    从声明里取到本批真实的目标消息编号。
    """

    def __init__(self, script: List[Any]) -> None:
        self.script = script
        self.messages: List[List[dict[str, Any]]] = []

    async def stream(self, messages=None, **kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        self.messages.append(list(messages or []))
        step = self.script[min(len(self.messages), len(self.script)) - 1]
        yield step(kwargs.get('tools') or []) if callable(step) else step


class _Replyer:
    """固定返回一句正文，并记录收到的消息序列。"""

    def __init__(self, text: str = '<say emotion="normal">写得快</say>') -> None:
        self.text = text
        self.messages: List[List[dict[str, Any]]] = []

    async def stream(self, messages=None, **_kwargs: Any) -> AsyncIterator[Dict[str, str]]:
        self.messages.append(list(messages or []))
        yield {'text': self.text}


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


def _call(name: str, **arguments: Any) -> Dict[str, Any]:
    return {'tool_calls': [{
        'id': f'call-{name}', 'name': name,
        'arguments': json.dumps(arguments, ensure_ascii=False),
    }]}


def _reply_call(mind: str):
    """回复本批最后一条消息；目标编号取自 reply 工具声明的取值。"""

    def build(tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        reply = next(tool['function'] for tool in tools if tool['function']['name'] == 'reply')
        target = reply['parameters']['properties']['target']['enum'][-1]
        return _call(
            'reply', mind=mind, target=target, reasons=['direct_question'],
            length='brief', reference='回一句',
        )

    return build


def _tool_chat(db, planner: _Planner, replyer: _Replyer, broker: _Broker | None = None) -> ChatService:
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.split_replyer = True
    config.conversation_agent.tool_calling = True
    config.conversation_agent.max_cognitive_rounds = 0
    config.conversation_agent.reply_quiet_seconds = 0
    config.conversation_agent.scene_batching = False
    config.group_chat.scene_refresh_messages = 0
    return ChatService(
        db, planner, None, None, _noop, cfg=config, broker=broker or _Broker(),
        planner_provider=planner, replyer_provider=replyer,
    )


def _group_member(registry: StreamRegistry, sender: str, card: str):
    return registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id=sender,
        sender_nickname=card,
        sender_group_card=card,
        first_seen_at=1_000_000,
    )


def _minds(chat: ChatService, stream_id: int) -> List[Dict[str, Any]]:
    return chat.memory.read_json(mind_key(stream_id), [])


async def _turn(chat: ChatService, stream_id: int) -> None:
    await chat._tick()
    await chat._inflight[stream_id].task


# ── 写入时机 ──────────────────────────────────────────────────────────


async def test_silent_writes_one_mind_record(db, monkeypatch) -> None:
    monkeypatch.setattr('src.core.agent.conversation.current_time', lambda: 1_800_000_000_000)
    planner = _Planner([_call('silent', mind='她们在聊作业，不是对我说的', reasons=['others_conversation'])])
    chat = _tool_chat(db, planner, _Replyer())
    context = _group_member(chat._registry, '97531', '小李')

    await chat.send(InboundMessage(text='月璃在吗', context=context, name_mentioned=True))
    await _turn(chat, context.stream.id)

    assert _minds(chat, context.stream.id) == [{
        'at': 1_800_000_000_000,
        'mind': '她们在聊作业，不是对我说的',
        'action': 'silent',
        'targetMessageId': None,
        'targetName': None,
        'turnId': event_store.search(kinds=['action_decision']).events[0]['turnId'],
    }]
    # silent 不是回应，不改动回应对象。
    assert chat.is_recent_partner(context.stream.id, context.person.id) is False


async def test_reply_writes_mind_after_delivery_with_target_name(db) -> None:
    broker = _Broker()
    chat = _tool_chat(db, _Planner([_reply_call('他在问我为什么用 py，回他')]), _Replyer(), broker)
    context = _group_member(chat._registry, '97531', '小李')
    await chat.send(InboundMessage(text='月璃你为啥选py写', context=context, name_mentioned=True))
    target_id = chat._buffers[context.stream.id][0].message_id

    await _turn(chat, context.stream.id)

    assert len(broker.dispatched) == 1
    [entry] = _minds(chat, context.stream.id)
    assert entry['action'] == 'reply'
    assert entry['mind'] == '他在问我为什么用 py，回他'
    assert entry['targetMessageId'] == target_id
    assert entry['targetName'] == '小李'
    assert chat.is_recent_partner(context.stream.id, context.person.id) is True


async def test_model_failure_writes_nothing(db) -> None:
    """模型不走工具通道（纠错一次仍如此）按 parse_error 终局，不写心理活动。"""
    planner = _Planner([{'text': '在的'}])
    chat = _tool_chat(db, planner, _Replyer())
    context = _group_member(chat._registry, '97531', '小李')

    await chat.send(InboundMessage(text='月璃在吗', context=context, name_mentioned=True))
    await _turn(chat, context.stream.id)

    assert len(planner.messages) == 2
    assert event_store.search(kinds=['action_decision']).events[0]['eventStatus'] == 'parse_error'
    assert _minds(chat, context.stream.id) == []


async def test_missing_mind_writes_nothing(db) -> None:
    planner = _Planner([_call('silent', reasons=['others_conversation'])])
    chat = _tool_chat(db, planner, _Replyer())
    context = _group_member(chat._registry, '97531', '小李')

    await chat.send(InboundMessage(text='月璃在吗', context=context, name_mentioned=True))
    await _turn(chat, context.stream.id)

    assert event_store.search(kinds=['action_decision']).events[0]['eventStatus'] == 'illegal_action'
    assert _minds(chat, context.stream.id) == []


class _ShadowProvider:
    """shadow 决策给出带 mind 的 silent，旧管线那一次给出正文。"""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, **_kwargs: Any) -> AsyncIterator[Dict[str, str]]:
        self.calls += 1
        if self.calls == 1:
            yield {'text': '<decision action="silent" mind="影子里的想法" reasons="others_conversation"/>'}
        else:
            yield {'text': '<say emotion="normal">在的</say>'}


async def test_shadow_decision_writes_nothing(db) -> None:
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'shadow'
    config.group_chat.scene_refresh_messages = 0
    provider = _ShadowProvider()
    chat = ChatService(db, provider, None, None, _noop, cfg=config, broker=_Broker())
    context = _group_member(chat._registry, '97531', '小李')

    await chat.send(InboundMessage(text='月璃在吗', context=context, name_mentioned=True))
    await _turn(chat, context.stream.id)

    assert provider.calls == 2
    assert _minds(chat, context.stream.id) == []


async def test_superseded_reply_writes_nothing_until_delivered(db) -> None:
    """生成期间发送者补发内容，回复作废、批次退回：作废的那一轮不写心理活动。"""
    provider = _InjectingProvider([[_reply('看什么呀')], [_reply('这个好看')]])
    chat = _xml_chat(db, provider, _RecordingBroker())
    context = _resolve(chat._registry)
    stream_id = context.stream.id

    async def send_follow_up() -> None:
        await chat.send(_text(context))

    provider.before_call[0] = send_follow_up
    await chat.send(_mention(context))
    await _run_turn(chat, stream_id)

    assert _minds(chat, stream_id) == []

    await _run_turn(chat, stream_id)

    assert [entry['action'] for entry in _minds(chat, stream_id)] == ['reply']


# ── 渲染 ──────────────────────────────────────────────────────────────


def _write_minds(chat: ChatService, stream_id: int, entries: List[MindEntry]) -> None:
    chat.memory.write_json(mind_key(stream_id), [entry.to_dict() for entry in entries])


async def test_mind_lines_interleave_by_time_only_in_decision_layer(db) -> None:
    replyer = _Replyer()
    planner = _Planner([_reply_call('小李在问我怎么看，接着回')])
    chat = _tool_chat(db, planner, replyer)
    asker = _group_member(chat._registry, '97531', '小李')
    lingbai = _group_member(chat._registry, '13579', '凌白')
    stream_id = asker.stream.id
    base = current_time() - 600_000
    question = chat.memory.append_message(stream_id, asker.person.id, 'user', '你为啥选py写', base)
    chat.memory.append_message(stream_id, lingbai.person.id, 'user', '因为', base + 60_000)
    chat.memory.append_message(stream_id, None, 'assistant', '<say>写得快</say>', base + 120_000)
    _write_minds(chat, stream_id, [
        # 早于工作记忆最早一条消息：对应的消息已不在眼前，不渲染。
        MindEntry(base - 3_600_000, '很早以前的想法', 'silent', None, None, 1),
        MindEntry(base + 1_000, '她在问凌白为什么用 py 写，不是问我，先看着', 'silent', None, None, 2),
        # 与她的回复同一毫秒：心里这行排在那句话之前。
        MindEntry(base + 120_000, '凌白只说了半句，我替他补上', 'reply', question, '小李', 3),
    ])
    await chat.send(InboundMessage(text='月璃你说呢', context=asker, name_mentioned=True))

    await _turn(chat, stream_id)

    decision_lines = [item['content'] for item in planner.messages[0][1:]]
    mind_lines = [line for line in decision_lines if line.startswith('[我·心里]')]
    assert len(mind_lines) == 2
    assert re.fullmatch(
        r'\[我·心里\] (\d\d-\d\d )?\d\d:\d\d 月璃心里想：'
        r'她在问凌白为什么用 py 写，不是问我，先看着（没有说话）',
        mind_lines[0],
    )
    assert mind_lines[1].endswith('月璃心里想：凌白只说了半句，我替他补上（回复了小李）')
    assert not any('很早以前的想法' in line for line in decision_lines)
    order = [
        next(index for index, line in enumerate(decision_lines) if marker in line)
        for marker in ('小李: 你为啥选py写', '先看着（没有说话）', '凌白: 因为', '我替他补上', '月璃: 写得快')
    ]
    assert order == sorted(order)
    # 心里行没有消息编号，也不进可选清单。
    protocol = decision_lines[-1]
    assert '心里想' not in protocol
    # 写正文那一环看不到历史里的心里行，只拿到这一轮的想法。
    replyer_text = '\n'.join(item['content'] for item in replyer.messages[0])
    assert '[我·心里]' not in replyer_text
    assert '你此刻心里的想法：小李在问我怎么看，接着回。这是想法，不要原样说出来。' in replyer_text


async def test_minds_stay_out_of_messages_table_and_fact_extraction(db) -> None:
    planner = _Planner([
        _reply_call('独一无二的回复想法'),
        _call('silent', mind='独一无二的沉默想法', reasons=['no_new_value']),
    ])
    chat = _tool_chat(db, planner, _Replyer())
    context = _group_member(chat._registry, '97531', '小李')
    stream_id = context.stream.id
    await chat.send(InboundMessage(text='月璃在吗', context=context, name_mentioned=True))
    await _turn(chat, stream_id)
    await chat.send(InboundMessage(text='月璃你看这个', context=context, name_mentioned=True))
    await _turn(chat, stream_id)

    assert [entry['mind'] for entry in _minds(chat, stream_id)] == [
        '独一无二的回复想法', '独一无二的沉默想法',
    ]
    contents = [row[0] for row in db.execute('SELECT content FROM messages')]
    assert not any('独一无二' in content for content in contents)
    dialogue = render_dialogue(chat.memory.messages_after(stream_id, 0, 100), [], '月璃')
    assert dialogue and '独一无二' not in dialogue


async def test_mind_log_is_capped_by_working_memory_window(db) -> None:
    planner = _Planner([_call('silent', mind='最新的想法', reasons=['no_new_value'])])
    chat = _tool_chat(db, planner, _Replyer())
    context = _group_member(chat._registry, '97531', '小李')
    window = chat._working_memory_messages
    _write_minds(chat, context.stream.id, [
        MindEntry(index, f'第{index}条', 'silent', None, None, index) for index in range(window)
    ])
    await chat.send(InboundMessage(text='月璃在吗', context=context, name_mentioned=True))

    await _turn(chat, context.stream.id)

    entries = _minds(chat, context.stream.id)
    assert len(entries) == window
    assert entries[0]['mind'] == '第1条'
    assert entries[-1]['mind'] == '最新的想法'


# ── 跟进窗口 ──────────────────────────────────────────────────────────


def test_recent_partner_is_unknown_until_an_effective_response(db) -> None:
    chat = _tool_chat(db, _Planner([]), _Replyer())

    assert chat.is_recent_partner(1, 1) is False


def test_speak_opens_the_window_to_everyone_and_reply_narrows_it(db) -> None:
    chat = _tool_chat(db, _Planner([]), _Replyer())

    chat._note_recent_partners(5, 'speak')
    assert chat.is_recent_partner(5, 1) and chat.is_recent_partner(5, 2)

    # 每次生效的回应整体替换：回复了 1 之后，只剩 1。
    chat._note_recent_partners(5, 'reply', [1])
    assert chat.is_recent_partner(5, 1) is True
    assert chat.is_recent_partner(5, 2) is False


async def test_legacy_pipeline_records_batch_sender_as_partner(db) -> None:
    config = Config()
    config.bot.name = '月璃'
    config.group_chat.scene_refresh_messages = 0

    class _Legacy:
        async def stream(self, **_kwargs: Any) -> AsyncIterator[Dict[str, str]]:
            yield {'text': '<say emotion="normal">在的</say>'}

    chat = ChatService(db, _Legacy(), None, None, _noop, cfg=config, broker=_Broker())
    context = _group_member(chat._registry, '97531', '小李')
    other = _group_member(chat._registry, '13579', '凌白')

    await chat.send(InboundMessage(text='月璃在吗', context=context, name_mentioned=True))
    await _turn(chat, context.stream.id)

    assert chat.is_recent_partner(context.stream.id, context.person.id) is True
    assert chat.is_recent_partner(context.stream.id, other.person.id) is False


async def test_entry_gate_and_batch_gate_read_the_same_partner_fact(db) -> None:
    """她刚回过小李：小李紧接着说的话两处门控都放行，凌白的话两处都不放行。"""
    chat = _tool_chat(db, _Planner([]), _Replyer())
    asker = _group_member(chat._registry, '97531', '小李')
    other = _group_member(chat._registry, '13579', '凌白')
    stream_id = asker.stream.id
    chat.memory.append_message(stream_id, None, 'assistant', '<say>写得快</say>', current_time() - 20_000)
    chat._note_recent_partners(stream_id, 'reply', [asker.person.id])

    original = (app_state.chat, app_state.registry, app_state.group_chat_config)
    try:
        app_state.chat = chat
        app_state.registry = chat._registry
        app_state.group_chat_config = GroupChatConfig()
        for sender, card in (('97531', '小李'), ('13579', '凌白')):
            await platform_inbound(PlatformInboundBody(
                platform='qq', streamKind='group', streamExternalId='86420',
                senderExternalId=sender, senderNickname=card, senderGroupCard=card,
                text='哈哈', mentionedMe=False, externalMessageId=f'in-{sender}',
            ))
    finally:
        app_state.chat, app_state.registry, app_state.group_chat_config = original

    gates = {
        event['personId']: event
        for event in event_store.search(kinds=['reply_gate']).events
    }
    assert gates[asker.person.id]['fromRecentPartner'] is True
    assert 'natural_reply_window' in gates[asker.person.id]['reasonCodes']
    assert gates[other.person.id]['fromRecentPartner'] is False
    assert gates[other.person.id]['reasonCodes'] == ['attention_filtered']

    for person, expected in ((asker, True), (other, False)):
        batch_gate = chat._batch_gate(
            person, '哈哈', False, sender_person_ids=[person.person.id],
        )
        assert ('natural_reply_window' in batch_gate.result.reason_codes) is expected
    # 按场面取批：批内任一发送者是最近回应过的人即放行。
    mixed = chat._batch_gate(
        other, '哈哈', False, sender_person_ids=[other.person.id, asker.person.id],
    )
    assert 'natural_reply_window' in mixed.result.reason_codes


# ── 可选清单附注 ──────────────────────────────────────────────────────


def _buffered(context, message_id: int, **facts: bool) -> _BufferedMessage:
    return _BufferedMessage(
        text='消息', context=context, mentioned_me=facts.get('mentioned_me', False),
        external_message_id=None, bot_name=None, message_id=message_id,
        previous_message_at=None, accepted_at=0,
        poked_me=facts.get('poked_me', False),
        replied_to_me=facts.get('replied_to_me', False),
        name_mentioned=facts.get('name_mentioned', False),
    )


def test_selectable_notes_state_who_each_message_is_for(db) -> None:
    chat = _tool_chat(db, _Planner([]), _Replyer())
    context = _group_member(chat._registry, '97531', '小李')
    batch = [
        _buffered(context, 1, mentioned_me=True),
        _buffered(context, 2, replied_to_me=True),
        _buffered(context, 3, name_mentioned=True),
        _buffered(context, 4, poked_me=True),
        _buffered(context, 5, mentioned_me=True, name_mentioned=True),
        _buffered(context, 6),
    ]

    assert chat._selectable_message_notes(batch, context) == {
        1: '（@了你）',
        2: '（引用了你的消息）',
        3: '（叫了你的名字）',
        4: '（戳了你）',
        5: '（@了你、叫了你的名字）',
        6: '（没有引用你、没有 @ 你、也没有叫你）',
    }


def test_direct_chat_has_no_addressee_note(db) -> None:
    chat = _tool_chat(db, _Planner([]), _Replyer())
    context = chat._registry.resolve_inbound(
        platform='qq', stream_kind='direct', stream_external_id='900000001',
        sender_external_id='900000001', sender_nickname='凌白', sender_group_card='',
        first_seen_at=1_000_000,
    )

    assert chat._selectable_message_notes([_buffered(context, 1)], context) == {}


def test_already_responded_message_carries_a_reminder(db) -> None:
    chat = _tool_chat(db, _Planner([]), _Replyer())
    context = _group_member(chat._registry, '97531', '小李')
    _write_minds(chat, context.stream.id, [
        MindEntry(1, '回他', 'reply', 7, '小李', 1),
        MindEntry(2, '先看着', 'silent', None, None, 2),
    ])

    notes = chat._selectable_message_notes(
        [_buffered(context, 7, mentioned_me=True), _buffered(context, 8, mentioned_me=True)],
        context,
    )

    assert notes == {7: f'（@了你）{RESPONDED_NOTE}', 8: '（@了你）'}


async def test_rendered_protocol_lists_notes_next_to_each_message(db) -> None:
    planner = _Planner([_call('silent', mind='凌白在跟小李说话，先看着', reasons=['others_conversation'])])
    chat = _tool_chat(db, planner, _Replyer())
    context = _group_member(chat._registry, '97531', '小李')

    await chat.send(InboundMessage(text='月璃在吗', context=context, name_mentioned=True))
    message_id = chat._buffers[context.stream.id][0].message_id
    await _turn(chat, context.stream.id)

    protocol = planner.messages[0][-1]['content']
    assert f'  {message_id} = 小李: 月璃在吗（叫了你的名字）' in protocol
