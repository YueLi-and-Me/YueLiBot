"""临时语调按每次开口抽取，连续聊天与主动搭话都不得沿用会话缓存。"""

from typing import Any, AsyncIterator, Dict, List
from unittest.mock import Mock

import json
import random

import pytest

from src.core.agent import character
from src.core.config.schema import Config
from src.core.platform_io.types import DeliveryReceipt, InboundMessage, OutboundMessage
from src.core.services.chat import ChatService


TONES = ['本次用古风说话', '本次用俏皮语气说话']


class _Provider:
    """记录实际提示词，决策与正文由测试指定。"""

    def __init__(self) -> None:
        self.response = ''
        self.messages: List[List[Dict[str, Any]]] = []
        self.target_id = 0

    async def stream(
        self, messages: List[Dict[str, Any]], **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        self.messages.append(messages)
        if kwargs.get('tools'):
            yield {'tool_calls': [{
                'id': 'reply-call',
                'name': 'reply',
                'arguments': json.dumps({
                    'target': self.target_id,
                    'reasons': ['direct_question'],
                    'length': 'brief',
                    'reference': '自然接话',
                }),
            }]}
        else:
            yield {'text': self.response}


class _Broker:
    """保留成功投递的气泡，以确认测试经过完整回复链路。"""

    def __init__(self) -> None:
        self.messages: List[OutboundMessage] = []

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        self.messages.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=['reply-id'],
        )


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


@pytest.fixture
def tone_rng(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """固定为命中、未命中、命中，避免概率测试偶然通过。"""
    rng = Mock(spec=random.Random)
    rng.random.side_effect = [0.1, 0.9, 0.1]
    rng.choice.side_effect = TONES
    # 只替换语调模块的随机源，不影响会话编号与其他业务的随机行为。
    monkeypatch.setattr(character, 'random', rng)
    return rng


@pytest.mark.parametrize('mode', ['legacy', 'single', 'split', 'tool'])
async def test_each_reply_selects_tone_within_same_session(db, tone_rng: Mock, mode: str) -> None:
    """同一会话的三次回复分别命中、恢复默认、命中另一候选；每次含两个气泡。"""
    config = Config()
    config.bot.name = '月璃'
    config.personality.reply_style = '平常简短自然地说话'
    config.personality.tone_probability = 0.5
    config.personality.tone_variants = TONES
    config.conversation_agent.mode = 'off' if mode == 'legacy' else 'enabled'
    config.conversation_agent.split_replyer = mode in ('split', 'tool')
    config.conversation_agent.tool_calling = mode == 'tool'
    planner, replyer, broker = _Provider(), _Provider(), _Broker()
    chat = ChatService(
        db, planner, None, None, _noop, cfg=config, broker=broker,
        planner_provider=planner, replyer_provider=replyer,
    )
    context = chat._registry.resolve_inbound(
        platform='qq', stream_kind='group', stream_external_id='10001',
        sender_external_id='20001', sender_nickname='小李', sender_group_card='',
        first_seen_at=1_000_000,
    )
    systems: List[str] = []
    session_starts: List[int | None] = []
    for index in range(3):
        await chat.send(InboundMessage(
            text=f'月璃，第{index + 1}个问题', context=context, name_mentioned=True,
        ))
        planner.target_id = chat.memory.latest_message_id(context.stream.id)
        body = f'<say>第{index + 1}个回答</say><say>还有第{index + 1}点补充</say>'
        head = (
            f'<decision action="reply" targets="{planner.target_id}" '
            'reasons="direct_question" length="brief" reference="自然接话"/>'
        )
        planner.response = body if mode == 'legacy' else head
        if mode == 'single':
            planner.response += body
        replyer.response = body
        await chat._tick()
        await chat._inflight[context.stream.id].task
        speaking_provider = replyer if mode in ('split', 'tool') else planner
        systems.append(speaking_provider.messages[-1][0]['content'])
        session_starts.append(chat._sessions[context.stream.id].started_at)

    assert len(set(session_starts)) == 1, '连续开口没有重新创建会话'
    assert [[tone in system for tone in TONES] for system in systems] == [
        [True, False], [False, False], [False, True],
    ]
    assert all(config.personality.reply_style in system for system in systems)
    assert tone_rng.random.call_count == 3
    assert tone_rng.choice.call_count == 2
    assert [message.segments for message in broker.messages] == [
        [f'第{index + 1}个回答', f'还有第{index + 1}点补充'] for index in range(3)
    ]
    if mode in ('split', 'tool'):
        assert all(
            tone not in messages[0]['content']
            for messages in planner.messages for tone in TONES
        ), '动作决策不应注入临时语调'


async def test_each_proactive_opening_selects_tone(db, tone_rng: Mock) -> None:
    """同一会话里的主动搭话也逐次抽取，不继承上次开口的临时语调。"""
    config = Config()
    config.personality.tone_probability = 0.5
    config.personality.tone_variants = TONES
    provider = _Provider()
    provider.response = '<say>歇一会儿吧</say>'
    chat = ChatService(db, None, provider, None, _noop, cfg=config)
    context = chat.desktop_context
    # 留一条历史，确保连续主动机会始终属于同一会话。
    await chat.send(InboundMessage(text='我在写代码', context=context))

    for _ in range(3):
        assert await chat.compose_proactive(context, '他写代码有一阵了', raise_model_errors=True)

    assert [[tone in messages[0]['content'] for tone in TONES] for messages in provider.messages] == [
        [True, False], [False, False], [False, True],
    ]
    assert tone_rng.random.call_count == 3
    assert tone_rng.choice.call_count == 2
