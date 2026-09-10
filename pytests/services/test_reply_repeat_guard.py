"""发送前按完整台词去重；用真实消息存储与事件账本验证投递边界。"""

from difflib import SequenceMatcher
from typing import Any, AsyncIterator, Dict, List

import asyncio
import pytest

from src.core.agent.parser import ResponseParser
from src.core.agent.prompt import render_replyer_protocol
from src.core.agent.segmentation import split_into_bubbles
from src.core.config.schema import Config
from src.core.logging.log_display import display_value
from src.core.observe.store import event_store
from src.core.platform_io.types import DeliveryReceipt, InboundMessage, OutboundMessage
from src.core.services.chat import ChatService
from src.core.services.chat import outbound
from src.core.services.chat.state import _TurnSink


class _Provider:
    response = ''

    async def stream(self, **_kwargs: Any) -> AsyncIterator[Dict[str, str]]:
        # 拆到单字，确保护栏在完整 say 结束后判断，而不是拿 chunk 比较。
        for char in self.response:
            yield {'text': char}


class _Broker:
    def __init__(self) -> None:
        self.dispatched: List[OutboundMessage] = []

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        self.dispatched.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform, stream_id=message.stream.id,
            external_message_ids=[],
        )


@pytest.fixture
def harness(db):
    provider = _Provider()
    broker = _Broker()
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.max_cognitive_rounds = 0
    pushed = []

    async def push(channel, payload, stream_id):
        pushed.append((channel, payload, stream_id))

    chat = ChatService(db, provider, None, None, push, cfg=config, broker=broker)
    context = chat._registry.resolve_inbound(
        platform='qq', stream_kind='direct', stream_external_id='repeat-test',
        sender_external_id='repeat-user', sender_nickname='测试用户',
        sender_group_card='', first_seen_at=1,
    )

    async def send(*lines: str) -> int:
        await chat.send(InboundMessage(text='月璃你看这个', context=context))
        target = chat.memory.latest_message_id(context.stream.id)
        provider.response = (
            f'<decision action="reply" targets="{target}" reasons="direct_question" length="brief"/>'
            + ''.join(f'<say emotion="smile">{line}</say>' for line in lines)
        )
        await chat._tick()
        await chat._inflight[context.stream.id].task
        return event_store.search(stream_id=context.stream.id, kinds=['llm_final']).events[0]['turnId']

    return chat, context, broker, send, pushed


def _blocked():
    return list(reversed(event_store.search(kinds=['reply_say_blocked']).events))


@pytest.mark.parametrize('lines', [
    ('心虚了是吧', '传销群主别装无辜'),
    ('哇...13抽就出了，运气真好', '蹭蹭欧气...'),
])
async def test_exact_repeat_never_reaches_delivery(harness, lines) -> None:
    chat, context, broker, send, _ = harness
    previous_turn = await send(*lines)
    await send(*lines)
    assert len(broker.dispatched) == 1, '逐字重复的第二轮进入了投递'
    blocked = _blocked()
    assert len(blocked) == len(lines)
    assert [entry['blockedText'] for entry in blocked] == list(lines)
    assert all(entry['similarity'] == 1.0 for entry in blocked)
    assert all(entry['matchedTurnId'] == previous_turn for entry in blocked)
    assert [entry['matchedText'] for entry in blocked] == list(lines)
    assert len([m for m in chat.memory.working_memory(context.stream.id) if m.role == 'assistant']) == 1


async def test_near_repeat_is_blocked(harness) -> None:
    _, _, broker, send, _ = harness
    previous, current = '传销群主别装无辜', '传销群主别装无辜呀'
    ratio = SequenceMatcher(None, previous, current).ratio()
    assert 0.9 <= ratio < 1.0
    await send(previous)
    await send(current)
    assert len(broker.dispatched) == 1, '近似复读进入了投递'
    assert _blocked()[0]['similarity'] == ratio
    assert _blocked()[0]['blockedText'] == current


async def test_same_topic_with_different_wording_is_delivered(harness) -> None:
    _, _, broker, send, _ = harness
    previous, current = '传销群主别装无辜', '群主这套说辞可骗不了我'
    assert SequenceMatcher(None, previous, current).ratio() < 0.9
    await send(previous)
    await send(current)
    assert [m.segments for m in broker.dispatched] == [[previous], [current]]
    assert _blocked() == []


async def test_only_repeated_say_is_dropped(harness) -> None:
    chat, context, broker, send, _ = harness
    await send('心虚了是吧')
    await send('心虚了是吧', '截图里还有一句没解释清楚')
    assert broker.dispatched[-1].segments == ['截图里还有一句没解释清楚'], '没有按单条 say 丢弃'
    assert chat.memory.working_memory(context.stream.id)[-1].content == '<say>截图里还有一句没解释清楚</say>'
    assert len(_blocked()) == 1


def test_rendered_replyer_contains_whole_sentence_rule() -> None:
    prompt = render_replyer_protocol('回应紧接着的新消息', 'brief')
    assert '不要重复自己最近说过的话，同样的意思必须换一种说法' in prompt
    assert '整句复述' in prompt
    assert '句式或口头禅' in prompt


async def test_all_says_dropped_without_replacement(harness) -> None:
    chat, context, broker, send, _ = harness
    await send('心虚了是吧', '传销群主别装无辜')
    history_before = [m.content for m in chat.memory.working_memory(context.stream.id) if m.role == 'assistant']
    await send('心虚了是吧', '传销群主别装无辜')
    assert len(broker.dispatched) == 1, '全部被丢弃时仍产生了正文投递'
    assert [m.content for m in chat.memory.working_memory(context.stream.id) if m.role == 'assistant'] == history_before
    assert len(_blocked()) == 2


async def test_original_say_boundary_survives_bubble_splitting(harness) -> None:
    chat, context, broker, send, _ = harness
    text = '今天这张截图里藏了好多细节，左边那个人还偷偷把抽奖结果遮起来了'
    expected = split_into_bubbles(text, chat._cfg.typing)
    assert len(expected) > 1
    await send(text)
    assert broker.dispatched[0].segments == expected
    assert chat.memory.working_memory(context.stream.id)[-1].content == f'<say>{text}</say>'
    await send(text)
    assert len(broker.dispatched) == 1
    assert _blocked()[0]['similarity'] == 1.0


async def test_history_window_counts_says_not_user_messages_or_actions(harness) -> None:
    chat, context, broker, send, _ = harness
    await send('心虚了是吧')
    message_id = chat.memory.working_memory(context.stream.id)[-1].message_id
    # 用户刷屏与纯表情不会挤掉最近的 Bot 台词。
    for _ in range(50):
        chat.memory.append_message(context.stream.id, context.person.id, 'user', '新的群消息')
        chat.memory.append_message(context.stream.id, None, 'assistant', '<emoji emotion="笑"/>')
    await send('心虚了是吧')
    assert len(broker.dispatched) == 1
    assert _blocked()[0]['matchedMessageId'] == message_id
    # 一条历史消息有 N 条 say 时，每条都占窗口，窗口外的同文允许再次发出。
    chat.memory.append_message(
        context.stream.id, None, 'assistant',
        ''.join(f'<say>另一个话题的第{i}条台词</say>' for i in range(outbound.REPLY_REPEAT_RECENT_SAYS)),
    )
    await send('心虚了是吧')
    assert len(broker.dispatched) == 2


async def test_other_stream_and_user_same_text_do_not_block(harness) -> None:
    chat, context, broker, send, _ = harness
    other = chat._registry.resolve_inbound(
        platform='qq', stream_kind='group', stream_external_id='other-group',
        sender_external_id='repeat-user', sender_nickname='测试用户',
        sender_group_card='', first_seen_at=1,
    )
    chat.memory.append_message(other.stream.id, None, 'assistant', '<say>心虚了是吧</say>')
    chat.memory.append_message(context.stream.id, context.person.id, 'user', '<say>心虚了是吧</say>')
    await send('心虚了是吧')
    assert len(broker.dispatched) == 1
    assert _blocked() == []


async def test_legacy_history_has_real_message_id_and_null_turn(harness) -> None:
    chat, context, broker, send, _ = harness
    message_id = chat.memory.append_message(context.stream.id, None, 'assistant', '<say>心虚了是吧</say>')
    await send('心虚了是吧')
    assert broker.dispatched == []
    assert _blocked()[0]['matchedMessageId'] == message_id
    assert _blocked()[0]['matchedTurnId'] is None
    assert _blocked()[0]['matchedTurnStatus'] == '未知'
    assert display_value(None, 'matchedTurnId') == '未知'


async def test_desktop_text_and_audio_wait_for_guard(harness) -> None:
    chat, _, _, _, pushed = harness
    context = chat._registry.desktop_context()
    chat.memory.append_message(context.stream.id, None, 'assistant', '<say>心虚了是吧</say>')
    spoken = []
    chat._speak_audio = lambda text, turn: spoken.append(text)
    sink = _TurnSink(context, asyncio.Event(), 99, 1, '测试')
    parser = ResponseParser()
    await chat._consume_events(parser.push('<say>心虚了是吧'), sink)
    assert pushed == []
    assert spoken == []
    await chat._consume_events(parser.push('</say><say>截图还有新的线索</say>'), sink)
    assert spoken == ['截图还有新的线索']
    visible = [p['event']['value'] for channel, p, _ in pushed if channel == 'chat.event' and p['event']['type'] == 'text']
    assert visible == ['截图还有新的线索']
    assert len(_blocked()) == 1
