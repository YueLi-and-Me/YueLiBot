"""验证 QQ 语音从逐条决策、TTS 合成到适配器消息段的完整离线链路。"""

from base64 import b64encode
from typing import Any, AsyncIterator, Dict, List
from unittest.mock import AsyncMock

import asyncio
import json

import pytest

from src.core.agent.parser import ResponseParser, ResponseProtocolError, SayEvent
from src.core.config.schema import Config, ModelCandidate
from src.core.llm_models.router import ModelRouter
from src.core.platform_io.broker import PlatformBroker
from src.core.platform_io.driver import DeliveryError
from src.core.platform_io.drivers.qq_ws import QqWebSocketDriver
from src.core.platform_io.types import InboundMessage, OutboundMessage, StreamRef
from src.core.platform_io.voice import MAX_VOICE_BYTES, validate_voice_data
from src.core.services.chat import ChatService
from src.core.services.chat.helpers import _extract_lines
from src.core.services.chat.state import _TurnSink
from src.core.services.media.tts import TtsService
from src.platforms.onebot11.backend import _parse_outbound
from src.platforms.onebot11.segments import outbound_message_batches


class _Provider:
    """给出可控决策和台词，记录实际交给模型的提示词。"""

    def __init__(self) -> None:
        self.response = ''
        self.messages: List[List[Dict[str, Any]]] = []
        self.target_id = 0

    async def stream(self, messages: List[Dict[str, Any]], **kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        self.messages.append(messages)
        if kwargs.get('tools'):
            yield {'tool_calls': [{
                'id': 'reply-call', 'name': 'reply',
                'arguments': json.dumps({
                    'mind': '用语音回答问候，文字补充链接', 'target': self.target_id,
                    'reasons': ['direct_question'], 'length': 'brief', 'reference': '用户想听语音',
                }),
            }]}
        else:
            # 将属性和正文切开，确保选择方式不依赖完整标签一次到达。
            for part in (self.response[:15], self.response[15:]):
                yield {'text': part}


async def _noop(*args: Any) -> None:
    return None


@pytest.mark.parametrize('mode', ['legacy', 'single', 'split', 'tool'])
@pytest.mark.parametrize('kind', ['direct', 'group'])
async def test_mixed_voice_reply_reaches_adapter_in_order(db, mode: str, kind: str) -> None:
    config = Config()
    config.tts.qq_enabled = True
    config.tts.voice = 'test-voice'
    config.typing.bubble_target_chars = 5
    config.typing.delay_enabled = False
    config.conversation_agent.mode = 'off' if mode == 'legacy' else 'enabled'
    config.conversation_agent.split_replyer = mode in ('split', 'tool')
    config.conversation_agent.tool_calling = mode == 'tool'
    planner, replyer = _Provider(), _Provider()
    pushes: List[Dict[str, Any]] = []

    async def push(stream_id: int, channel: str, payload: Any) -> int:
        pushes.append({'stream_id': stream_id, 'channel': channel, 'payload': payload})
        return 1

    broker = PlatformBroker()
    chat = ChatService(
        db, planner, None, None, _noop, cfg=config, broker=broker,
        planner_provider=planner, replyer_provider=replyer,
    )
    context = chat._registry.resolve_inbound(
        platform='qq', stream_kind=kind, stream_external_id='10001',
        sender_external_id='20001', sender_nickname='小李', sender_group_card='', first_seen_at=1,
    )
    broker.register(context.stream.id, QqWebSocketDriver(push))
    synth = AsyncMock(return_value=b'voice-bytes')
    chat._synthesize_audio = synth
    chat.set_platform_capabilities('qq', ['send_record'])
    await chat.send(InboundMessage(text='月璃，请用语音问好', context=context, name_mentioned=True))
    planner.target_id = chat.memory.latest_message_id(context.stream.id)
    voice_text = '早上好，今天也要好好休息。'
    body = f'<say delivery="voice">{voice_text}</say><say>链接在这</say>'
    head = (
        f'<decision action="reply" mind="回应语音请求" targets="{planner.target_id}" '
        'reasons="direct_question" length="brief" reference="自然接话"/>'
    )
    planner.response = body if mode == 'legacy' else head
    if mode == 'single':
        planner.response += body
    replyer.response = body
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert len(pushes) == 1
    outbound = _parse_outbound(pushes[0])
    assert outbound.segments == [voice_text, '链接在这']
    assert outbound.voice_data == (b64encode(b'voice-bytes').decode(), '')
    batches = outbound_message_batches(
        outbound.segments, (), (), voice_data=outbound.voice_data,
    )
    assert batches == [
        [{'type': 'record', 'data': {'file': 'base64://dm9pY2UtYnl0ZXM='}}],
        [{'type': 'text', 'data': {'text': '链接在这'}}],
    ]
    synth.assert_awaited_once_with(voice_text)
    speaker = replyer if mode in ('split', 'tool') else planner
    assert 'delivery="voice"' in speaker.messages[-1][0]['content']


@pytest.mark.parametrize('enabled,capable,callback', [(False, True, True), (True, False, True), (True, True, False)])
async def test_unavailable_voice_is_rejected_before_synthesis(db, enabled, capable, callback) -> None:
    config = Config()
    config.tts.qq_enabled = enabled
    chat = ChatService(db, _Provider(), None, None, _noop, cfg=config)
    context = chat._registry.resolve_inbound(
        platform='qq', stream_kind='direct', stream_external_id='10001',
        sender_external_id='20001', sender_nickname='小李', sender_group_card='', first_seen_at=1,
    )
    synth = AsyncMock(return_value=b'audio')
    chat._synthesize_audio = synth if callback else None
    chat.set_platform_capabilities('qq', ['send_record'] if capable else [])
    sink = _TurnSink(context, asyncio.Event(), 1, 1, '请说话')
    parser = ResponseParser()
    with pytest.raises(ResponseProtocolError, match='未启用语音发送'):
        await chat._consume_events(parser.push('<say delivery="voice">你好</say>'), sink)
    synth.assert_not_called()
    assert sink.segments == []
    assert '不得使用' in chat._voice_protocol(context)


async def test_synthesis_failure_does_not_send_partial_text(db) -> None:
    config = Config()
    config.tts.qq_enabled = True
    push = AsyncMock(return_value=1)
    broker = PlatformBroker()
    chat = ChatService(db, _Provider(), None, None, _noop, cfg=config, broker=broker)
    context = chat._registry.resolve_inbound(
        platform='qq', stream_kind='direct', stream_external_id='10001',
        sender_external_id='20001', sender_nickname='小李', sender_group_card='', first_seen_at=1,
    )
    broker.register(context.stream.id, QqWebSocketDriver(push))
    chat.set_platform_capabilities('qq', ['send_record'])
    chat._synthesize_audio = AsyncMock(side_effect=RuntimeError('厂商拒绝合成'))
    with pytest.raises(DeliveryError, match='厂商拒绝合成'):
        await chat._dispatch_outbound(context, 1, ['文字', '语音'], [], voice_segments=[False, True])
    push.assert_not_called()


async def test_tts_qq_switch_independent_of_desktop_and_cache_config(monkeypatch) -> None:
    config = Config()
    config.tts.qq_enabled = True
    config.tts.voice = 'test-voice'
    router = ModelRouter('tts', [ModelCandidate(
        name='voice', provider='test', kind='openai', identifier='voice-test', base_url='https://example.test',
    )])
    push = AsyncMock()
    service = TtsService(config, push, router)
    synth = AsyncMock(return_value=b'audio')
    monkeypatch.setattr(service, '_synth', synth)
    assert not service.enabled
    service.speak('不播放', 1)
    assert await service.synthesize('你好') == b'audio'
    assert await service.synthesize('你好') == b'audio'
    assert synth.await_count == 1
    config.tts.speed = 1.2
    await service.synthesize('你好')
    config.tts.format = 'wav'
    await service.synthesize('你好')
    assert synth.await_count == 3
    push.assert_not_called()
    config.tts.qq_enabled = False
    with pytest.raises(RuntimeError, match='未启用'):
        await service.synthesize('你好')


@pytest.mark.parametrize('value', ['sound', '', 'VOICE'])
def test_unknown_delivery_is_not_silently_text(value: str) -> None:
    with pytest.raises(ResponseProtocolError, match='delivery'):
        ResponseParser().push(f'<say delivery="{value}">你好</say>')


def test_default_delivery_is_text() -> None:
    events = ResponseParser().push('<say>你好</say>')
    assert next(item for item in events if isinstance(item, SayEvent)).delivery == 'text'


@pytest.mark.parametrize('data,count', [(['abc'], 1), (['data:audio/mp3;base64,AA=='], 1), (['AA=='], 2), ([123], 1)])
def test_malformed_audio_is_rejected(data, count: int) -> None:
    with pytest.raises(ValueError):
        validate_voice_data(data, count)


def test_aggregate_audio_limit() -> None:
    part = b64encode(b'a' * (MAX_VOICE_BYTES // 2 + 1)).decode()
    with pytest.raises(ValueError, match='超过'):
        OutboundMessage(StreamRef(1, 'qq', 'direct', '10001'), ['a', 'b'], voice_data=(part, part))


async def test_interruption_during_synthesis_prevents_delivery(db) -> None:
    config = Config()
    config.tts.qq_enabled = True
    push = AsyncMock(return_value=1)
    broker = PlatformBroker()
    chat = ChatService(db, _Provider(), None, None, _noop, cfg=config, broker=broker)
    context = chat._registry.resolve_inbound(
        platform='qq', stream_kind='direct', stream_external_id='10001',
        sender_external_id='20001', sender_nickname='小李', sender_group_card='', first_seen_at=1,
    )
    broker.register(context.stream.id, QqWebSocketDriver(push))
    chat.set_platform_capabilities('qq', ['send_record'])
    cancelled = asyncio.Event()

    async def synthesize(_text: str) -> bytes:
        cancelled.set()
        return b'audio'

    chat._synthesize_audio = synthesize
    with pytest.raises(asyncio.CancelledError):
        await chat._dispatch_outbound(
            context, 1, ['语音'], [], voice_segments=[True], cancel_event=cancelled,
        )
    push.assert_not_called()


async def test_proactive_voice_keeps_delivery_choice(db) -> None:
    config = Config()
    config.tts.qq_enabled = True
    push = AsyncMock(return_value=1)
    broker = PlatformBroker()
    chat = ChatService(db, _Provider(), None, None, _noop, cfg=config, broker=broker)
    context = chat._registry.resolve_inbound(
        platform='qq', stream_kind='direct', stream_external_id='10001',
        sender_external_id='20001', sender_nickname='小李', sender_group_card='', first_seen_at=1,
    )
    broker.register(context.stream.id, QqWebSocketDriver(push))
    chat.set_platform_capabilities('qq', ['send_record'])
    chat._synthesize_audio = AsyncMock(return_value=b'audio')
    lines = _extract_lines('<say delivery="voice">去吃午饭吧</say>')
    assert lines is not None
    assert chat.claim_stream(context.stream.id, 'proactive')
    try:
        await chat._speak_claimed_external(context, lines, turn=1)
    finally:
        chat.release_stream(context.stream.id, 'proactive')
    assert push.await_args.args[2]['voiceData'] == [b64encode(b'audio').decode()]
    assert 'delivery="voice"' in next(chat.memory.iter_assistant_messages(context.stream.id)).content
