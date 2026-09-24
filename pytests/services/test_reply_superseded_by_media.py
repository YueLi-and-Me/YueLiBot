"""回复生成期间发送者补发图片或视频时，当前回合回复作废并与补发内容合并。

QQ 中视频不能与文字同条发送，「@她 看这个」之后紧跟视频是常见发法。@ 那条一到
回合即开始，视频晚到一两秒会落入下一批；若不处理，第一回合只能针对「看这个」
作答。验收要点：补发图片或视频时第一回合不投递、不写历史，下一回合的批次同时
包含原消息与补发内容；补发文字、表情包或他人插话时照常投递；同一批至多退回一次；
已产生心情或约定副作用的回复不作废。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List

import pytest

from src.core.config.schema import Config
from src.core.observe.store import event_store
from src.core.platform_io.types import (
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
    VideoSource,
)
from src.core.services.chat import ChatService


class _InjectingProvider:
    """按调用序号返回脚本输出，并可在某次调用开始生成前注入入站消息。"""

    def __init__(self, scripts: List[List[str]]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.before_call: Dict[int, Callable[[], Awaitable[None]]] = {}

    async def stream(self, messages=None, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        index = self.calls
        self.calls += 1
        hook = self.before_call.get(index)
        if hook is not None:
            await hook()
        for text in self.scripts[min(index, len(self.scripts) - 1)]:
            yield {'text': text}


class _RecordingBroker:
    def __init__(self) -> None:
        self.dispatched: List[OutboundMessage] = []

    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        self.dispatched.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=['x'],
        )


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


def _config() -> Config:
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.max_cognitive_rounds = 0
    config.group_chat.scene_refresh_messages = 0
    return config


def _resolve(registry, *, kind: str = 'group', sender: str = '97531', card: str = '小李'):
    return registry.resolve_inbound(
        platform='qq',
        stream_kind=kind,
        stream_external_id='86420' if kind == 'group' else sender,
        sender_external_id=sender,
        sender_nickname=card,
        sender_group_card=card if kind == 'group' else '',
        first_seen_at=1_000_000,
    )


def _reply(text: str, extra: str = '') -> str:
    return (
        '<decision action="reply" targets="1" reasons="direct_question" length="brief"/>'
        f'{extra}<say emotion="normal">{text}</say>'
    )


def _image(context) -> InboundMessage:
    return InboundMessage(
        text='[图片]', context=context, image_sources=('https://example.invalid/a.png',),
    )


def _video(context) -> InboundMessage:
    return InboundMessage(
        text='[视频]',
        context=context,
        video_sources=(VideoSource(url='https://example.invalid/v', file='abc.mp4'),),
    )


def _sticker(context) -> InboundMessage:
    return InboundMessage(
        text='[表情包]',
        context=context,
        emoji_sources=('https://example.invalid/s.gif',),
        emoji_sub_types=(1,),
    )


def _user_inputs_by_turn() -> Dict[int, List[str]]:
    grouped: Dict[int, List[str]] = {}
    for event in reversed(event_store.search(kinds=['user_input']).events):
        grouped.setdefault(event['turnId'], []).append(event['text'])
    return grouped


def _assistant_lines(chat: ChatService, stream_id: int) -> List[str]:
    return [
        message.content
        for message in chat.memory.working_memory(stream_id, 20)
        if message.role == 'assistant'
    ]


async def _run_turn(chat: ChatService, stream_id: int) -> None:
    await chat._tick()
    await chat._inflight[stream_id].task


def _chat(db, provider: _InjectingProvider, broker: _RecordingBroker) -> ChatService:
    return ChatService(db, provider, None, None, _noop, cfg=_config(), broker=broker)


@pytest.mark.parametrize('follow_up', [_image, _video], ids=['image', 'video'])
async def test_media_from_same_sender_supersedes_reply_and_merges_batch(db, follow_up) -> None:
    provider = _InjectingProvider([[_reply('看什么呀')], [_reply('这个好看')]])
    broker = _RecordingBroker()
    chat = _chat(db, provider, broker)
    context = _resolve(chat._registry)
    stream_id = context.stream.id

    async def send_follow_up() -> None:
        await chat.send(follow_up(context))

    provider.before_call[0] = send_follow_up
    await chat.send(InboundMessage(text='@月璃 看这个', context=context, mentioned_me=True))
    await _run_turn(chat, stream_id)

    # 第一回合的回复不投递、不写历史，批次退回缓冲头部与补发内容相接。
    assert broker.dispatched == []
    assert _assistant_lines(chat, stream_id) == []
    assert [message.text for message in chat._buffers[stream_id]] == [
        '@月璃 看这个', follow_up(context).text,
    ]

    await _run_turn(chat, stream_id)

    assert provider.calls == 2
    assert [message.segments for message in broker.dispatched] == [['这个好看']]
    assert _assistant_lines(chat, stream_id) == ['<say>这个好看</say>']
    batches = list(_user_inputs_by_turn().values())
    assert batches[-1] == ['@月璃 看这个', follow_up(context).text]


@pytest.mark.parametrize(
    'follow_up',
    [_sticker, lambda context: InboundMessage(text='就是这个', context=context)],
    ids=['sticker', 'text'],
)
async def test_sticker_or_text_follow_up_does_not_supersede(db, follow_up) -> None:
    provider = _InjectingProvider([[_reply('在呢')], [_reply('嗯嗯')]])
    broker = _RecordingBroker()
    chat = _chat(db, provider, broker)
    context = _resolve(chat._registry)
    stream_id = context.stream.id

    async def send_follow_up() -> None:
        await chat.send(follow_up(context))

    provider.before_call[0] = send_follow_up
    await chat.send(InboundMessage(text='@月璃 在吗', context=context, mentioned_me=True))
    await _run_turn(chat, stream_id)

    assert [message.segments for message in broker.dispatched] == [['在呢']]
    assert [message.text for message in chat._buffers[stream_id]] == [follow_up(context).text]


async def test_media_after_another_sender_interjects_does_not_supersede(db) -> None:
    """中间插进他人消息时，补发内容与原批合并不到一起，作废没有意义。"""
    provider = _InjectingProvider([[_reply('看什么呀')]])
    broker = _RecordingBroker()
    chat = _chat(db, provider, broker)
    context = _resolve(chat._registry)
    other = _resolve(chat._registry, sender='24680', card='小王')
    stream_id = context.stream.id

    async def interject_then_media() -> None:
        await chat.send(InboundMessage(text='哈哈', context=other))
        await chat.send(_image(context))

    provider.before_call[0] = interject_then_media
    await chat.send(InboundMessage(text='@月璃 看这个', context=context, mentioned_me=True))
    await _run_turn(chat, stream_id)

    assert [message.segments for message in broker.dispatched] == [['看什么呀']]


async def test_merged_batch_is_not_superseded_again(db) -> None:
    """同一批至多退回一次：合并后的回合里对方再补图，照常投递。"""
    provider = _InjectingProvider([
        [_reply('看什么呀')], [_reply('这个好看')], [_reply('还有一张啊')],
    ])
    broker = _RecordingBroker()
    chat = _chat(db, provider, broker)
    context = _resolve(chat._registry)
    stream_id = context.stream.id

    async def send_image() -> None:
        await chat.send(_image(context))

    provider.before_call[0] = send_image
    provider.before_call[1] = send_image
    await chat.send(InboundMessage(text='@月璃 看这个', context=context, mentioned_me=True))
    await _run_turn(chat, stream_id)
    await _run_turn(chat, stream_id)

    assert [message.segments for message in broker.dispatched] == [['这个好看']]
    assert stream_id not in chat._waiting
    # 第二张图留在缓冲里，由之后的回合正常处理。
    assert [message.text for message in chat._buffers[stream_id]] == ['[图片]']


async def test_reply_with_mood_side_effect_is_not_superseded(db) -> None:
    """心情变化在生成时已落库，作废后下一回合会重复写入，因此照常投递。"""
    provider = _InjectingProvider([[_reply('看什么呀', '<mood favor="1"/>')]])
    broker = _RecordingBroker()
    chat = _chat(db, provider, broker)
    context = _resolve(chat._registry)
    stream_id = context.stream.id

    async def send_image() -> None:
        await chat.send(_image(context))

    provider.before_call[0] = send_image
    await chat.send(InboundMessage(text='@月璃 看这个', context=context, mentioned_me=True))
    await _run_turn(chat, stream_id)

    assert [message.segments for message in broker.dispatched] == [['看什么呀']]


async def test_direct_chat_image_follow_up_supersedes_reply(db) -> None:
    provider = _InjectingProvider([[_reply('看什么呀')], [_reply('这个好看')]])
    broker = _RecordingBroker()
    chat = _chat(db, provider, broker)
    context = _resolve(chat._registry, kind='direct')
    stream_id = context.stream.id

    async def send_image() -> None:
        await chat.send(_image(context))

    provider.before_call[0] = send_image
    await chat.send(InboundMessage(text='看这个', context=context))
    await _run_turn(chat, stream_id)
    assert broker.dispatched == []

    await _run_turn(chat, stream_id)
    assert [message.segments for message in broker.dispatched] == [['这个好看']]
