"""回复开口时机：开口前静默等待，以及生成期间发送者补发新内容时作废重来。

两条规则针对同一件事：回复是按回合开始时的批次写的，发送者还没说完就开写，写出的
话可能与他随后说的矛盾（「@她 看这个」之后一秒才到的视频、「光喊宝宝不说话」）。
- 静默等待：发送者发完最后一条后安静够 ``reply_quiet_seconds`` 秒才开始，
  连发的几条合成一批；后面已有别人的消息或是桌面端时不等。
- 作废重来：生成期间本批发送者补了文字、图片、视频或合并转发，本回合回复不投递、
  不写历史，批次退回缓冲与补发内容合并；只补表情包、QQ 表情、戳一戳或只 @ 一下
  不算。重来次数受 ``max_reply_restarts`` 限制，已产生心情或约定副作用的回复不作废。
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
from src.core.services.chat import service as chat_service_module


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


def _config(*, quiet: float = 0.0, restarts: int = 1) -> Config:
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.max_cognitive_rounds = 0
    config.conversation_agent.reply_quiet_seconds = quiet
    config.conversation_agent.max_reply_restarts = restarts
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


def _mention(context, text: str = '@月璃 看这个') -> InboundMessage:
    return InboundMessage(text=text, context=context, mentioned_me=True, authored_text=' 看这个')


def _image(context) -> InboundMessage:
    return InboundMessage(
        text='[图片]', context=context,
        image_sources=('https://example.invalid/a.png',), authored_text='',
    )


def _video(context) -> InboundMessage:
    return InboundMessage(
        text='[视频]',
        context=context,
        video_sources=(VideoSource(url='https://example.invalid/v', file='abc.mp4'),),
        authored_text='',
    )


def _text(context) -> InboundMessage:
    # 未单独提交用户原文（其他适配器）时，整段正文按用户所写处理。
    return InboundMessage(text='你变笨了我也爱你', context=context)


def _sticker(context) -> InboundMessage:
    return InboundMessage(
        text='[表情包]',
        context=context,
        emoji_sources=('https://example.invalid/s.gif',),
        emoji_sub_types=(1,),
        authored_text='',
    )


def _qq_face(context) -> InboundMessage:
    return InboundMessage(text='[表情：笑哭]', context=context, authored_text='')


def _poke(context) -> InboundMessage:
    return InboundMessage(text='[戳了戳月璃]', context=context, poked_me=True, authored_text='')


def _bare_mention(context) -> InboundMessage:
    return InboundMessage(text='@月璃', context=context, mentioned_me=True, authored_text='')


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


def _chat(db, provider: _InjectingProvider, broker: _RecordingBroker, **config: Any) -> ChatService:
    return ChatService(db, provider, None, None, _noop, cfg=_config(**config), broker=broker)


def _segments(broker: _RecordingBroker) -> List[List[str]]:
    return [message.segments for message in broker.dispatched]


class TestSupersede:
    @pytest.mark.parametrize(
        'follow_up', [_image, _video, _text], ids=['image', 'video', 'text'],
    )
    async def test_content_from_same_sender_supersedes_reply_and_merges_batch(
        self, db, follow_up,
    ) -> None:
        provider = _InjectingProvider([[_reply('看什么呀')], [_reply('这个好看')]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker)
        context = _resolve(chat._registry)
        stream_id = context.stream.id

        async def send_follow_up() -> None:
            await chat.send(follow_up(context))

        provider.before_call[0] = send_follow_up
        await chat.send(_mention(context))
        await _run_turn(chat, stream_id)

        # 第一回合的回复不投递、不写历史，批次退回缓冲头部与补发内容相接。
        assert broker.dispatched == []
        assert _assistant_lines(chat, stream_id) == []
        assert [message.text for message in chat._buffers[stream_id]] == [
            '@月璃 看这个', follow_up(context).text,
        ]
        assert chat._reply_restarts[stream_id] == 1
        # 作废不占用 wait 的标记：合并后的回合仍可以选择先等等。
        assert stream_id not in chat._waiting

        await _run_turn(chat, stream_id)

        assert provider.calls == 2
        assert _segments(broker) == [['这个好看']]
        assert _assistant_lines(chat, stream_id) == ['<say>这个好看</say>']
        assert list(_user_inputs_by_turn().values())[-1] == [
            '@月璃 看这个', follow_up(context).text,
        ]
        assert stream_id not in chat._reply_restarts

    @pytest.mark.parametrize(
        'follow_up',
        [_sticker, _qq_face, _poke, _bare_mention],
        ids=['sticker', 'qq_face', 'poke', 'bare_mention'],
    )
    async def test_reaction_only_follow_up_does_not_supersede(self, db, follow_up) -> None:
        provider = _InjectingProvider([[_reply('在呢')], [_reply('嗯嗯')]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker)
        context = _resolve(chat._registry)
        stream_id = context.stream.id

        async def send_follow_up() -> None:
            await chat.send(follow_up(context))

        provider.before_call[0] = send_follow_up
        await chat.send(_mention(context, '@月璃 在吗'))
        await _run_turn(chat, stream_id)

        assert _segments(broker) == [['在呢']]
        assert [message.text for message in chat._buffers[stream_id]] == [follow_up(context).text]

    async def test_follow_up_after_another_sender_interjects_does_not_supersede(self, db) -> None:
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
        await chat.send(_mention(context))
        await _run_turn(chat, stream_id)

        assert _segments(broker) == [['看什么呀']]

    async def test_restart_limit_one_delivers_the_merged_reply(self, db) -> None:
        """重来一次后达到上限：合并后的回合里对方再补一句，照常投递。"""
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
        await chat.send(_mention(context))
        await _run_turn(chat, stream_id)
        await _run_turn(chat, stream_id)

        assert _segments(broker) == [['这个好看']]
        assert stream_id not in chat._reply_restarts
        # 第二张图留在缓冲里，由之后的回合正常处理。
        assert [message.text for message in chat._buffers[stream_id]] == ['[图片]']

    async def test_restart_limit_two_allows_a_second_restart(self, db) -> None:
        provider = _InjectingProvider([
            [_reply('看什么呀')], [_reply('这个好看')], [_reply('两张都好看')],
        ])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker, restarts=2)
        context = _resolve(chat._registry)
        stream_id = context.stream.id

        async def send_image() -> None:
            await chat.send(_image(context))

        provider.before_call[0] = send_image
        provider.before_call[1] = send_image
        await chat.send(_mention(context))
        await _run_turn(chat, stream_id)
        await _run_turn(chat, stream_id)
        assert broker.dispatched == []
        assert chat._reply_restarts[stream_id] == 2

        await _run_turn(chat, stream_id)
        assert _segments(broker) == [['两张都好看']]
        assert list(_user_inputs_by_turn().values())[-1] == ['@月璃 看这个', '[图片]', '[图片]']

    async def test_restart_limit_zero_never_supersedes(self, db) -> None:
        provider = _InjectingProvider([[_reply('看什么呀')]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker, restarts=0)
        context = _resolve(chat._registry)
        stream_id = context.stream.id

        async def send_image() -> None:
            await chat.send(_image(context))

        provider.before_call[0] = send_image
        await chat.send(_mention(context))
        await _run_turn(chat, stream_id)

        assert _segments(broker) == [['看什么呀']]

    async def test_reply_with_mood_side_effect_is_not_superseded(self, db) -> None:
        """心情变化在生成时已落库，作废后下一回合会重复写入，因此照常投递。"""
        provider = _InjectingProvider([[_reply('看什么呀', '<mood favor="1"/>')]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker)
        context = _resolve(chat._registry)
        stream_id = context.stream.id

        async def send_image() -> None:
            await chat.send(_image(context))

        provider.before_call[0] = send_image
        await chat.send(_mention(context))
        await _run_turn(chat, stream_id)

        assert _segments(broker) == [['看什么呀']]

    async def test_direct_chat_text_follow_up_supersedes_reply(self, db) -> None:
        provider = _InjectingProvider([[_reply('在呢')], [_reply('问吧')]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker)
        context = _resolve(chat._registry, kind='direct')
        stream_id = context.stream.id

        async def send_text() -> None:
            await chat.send(InboundMessage(text='问你个事', context=context))

        provider.before_call[0] = send_text
        await chat.send(InboundMessage(text='在吗', context=context))
        await _run_turn(chat, stream_id)
        assert broker.dispatched == []

        await _run_turn(chat, stream_id)
        assert _segments(broker) == [['问吧']]


class TestQuietWindow:
    @pytest.fixture
    def clock(self, monkeypatch):
        state = {'now': 1_800_000_000_000}
        monkeypatch.setattr(chat_service_module, 'current_time', lambda: state['now'])
        return state

    async def test_turn_waits_until_sender_is_quiet(self, db, clock) -> None:
        provider = _InjectingProvider([[_reply('这个好看')]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker, quiet=1.5)
        context = _resolve(chat._registry)
        stream_id = context.stream.id

        await chat.send(_mention(context))
        clock['now'] += 1_025
        await chat._tick()
        assert stream_id not in chat._inflight

        # 视频在 @ 之后 1.025 秒到达，静默窗口从这一条重新计时。
        await chat.send(_video(context))
        clock['now'] += 1_000
        await chat._tick()
        assert stream_id not in chat._inflight

        clock['now'] += 500
        await _run_turn(chat, stream_id)
        assert list(_user_inputs_by_turn().values())[-1] == ['@月璃 看这个', '[视频]']

    async def test_turn_starts_without_waiting_when_another_sender_followed(self, db, clock) -> None:
        """后面已有别人的消息，发送者的这一段已经结束，不再等。"""
        provider = _InjectingProvider([[_reply('看什么呀')]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker, quiet=1.5)
        context = _resolve(chat._registry)
        other = _resolve(chat._registry, sender='24680', card='小王')
        stream_id = context.stream.id

        await chat.send(_mention(context))
        await chat.send(InboundMessage(text='哈哈', context=other))
        await _run_turn(chat, stream_id)
        assert list(_user_inputs_by_turn().values())[-1] == ['@月璃 看这个']

    async def test_zero_quiet_seconds_starts_immediately(self, db, clock) -> None:
        provider = _InjectingProvider([[_reply('在呢')]])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker, quiet=0.0)
        context = _resolve(chat._registry)
        stream_id = context.stream.id

        await chat.send(_mention(context, '@月璃 在吗'))
        await _run_turn(chat, stream_id)
        assert _segments(broker) == [['在呢']]

    async def test_desktop_does_not_wait(self, db, clock) -> None:
        provider = _InjectingProvider([['<say>在呢</say>']])
        broker = _RecordingBroker()
        chat = _chat(db, provider, broker, quiet=1.5)
        context = chat._registry.desktop_context()

        await chat.send(InboundMessage(text='在吗', context=context))
        await chat._tick()
        assert context.stream.id in chat._inflight
        await chat._inflight[context.stream.id].task


def test_schema_defaults_keep_old_behaviour_and_seed_enables_quiet_window() -> None:
    """缺键时静默为 0、重来上限为 1；新装种子写 1.5 秒。"""
    from src.core.config.bootstrap import _bot_document

    agent = Config().conversation_agent
    assert agent.reply_quiet_seconds == 0.0
    assert agent.max_reply_restarts == 1
    assert _bot_document()['conversation_agent']['reply_quiet_seconds'] == 1.5
