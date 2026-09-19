"""视频理解的范围（scope）决策与补看、以及同一条消息多种媒体的合并且与顺序无关。

全部用进程内替身：不连模型、不连协议端；消息体读写走真实的记忆层（内存 SQLite）。
"""

from __future__ import annotations

from typing import Any

import asyncio

import pytest

from src.core.config.schema import Config
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import InboundMessage, VideoSource
from src.core.services.chat import ChatService


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


class _FakeVideoDescriber:
    """记录调用、可按闸门放行的视频描述服务替身。"""

    def __init__(self, text: str = '两个人在打射击游戏，配音在说别抓我') -> None:
        self.text = text
        self.calls: list[tuple[VideoSource, ...]] = []
        self.gate = asyncio.Event()
        self.gate.set()

    async def describe_sources(self, sources: tuple[VideoSource, ...]) -> list[Any]:
        await self.gate.wait()
        self.calls.append(sources)
        return [self.text] * len(sources)


class _GatedImageDescriber:
    """记录调用、可按闸门放行的图片描述服务替身。"""

    def __init__(self, text: str = '一只猫') -> None:
        self.text = text
        self.calls: list[tuple[str, ...]] = []
        self.gate = asyncio.Event()

    async def describe_sources(self, sources: tuple[str, ...]) -> list[Any]:
        await self.gate.wait()
        self.calls.append(sources)
        return [self.text] * len(sources)

    async def describe_emoji_sources(self, sources: tuple[str, ...]) -> list[Any]:
        return [None] * len(sources)


def _config(**overrides: Any) -> Config:
    config = Config()
    config.bot.name = '月璃'
    config.vision.chat_video_enabled = True
    for key, value in overrides.items():
        setattr(config.vision, key, value)
    return config


def _group_context(registry: StreamRegistry):
    return registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='小李',
        first_seen_at=1_000_000,
    )


def _direct_context(registry: StreamRegistry):
    return registry.resolve_inbound(
        platform='qq',
        stream_kind='direct',
        stream_external_id='900000001',
        sender_external_id='900000001',
        sender_nickname='凌白',
        sender_group_card='',
        first_seen_at=1_000_000,
    )


def _video_message(
    context: Any,
    text: str = '看看这个[视频]',
    **kwargs: Any,
) -> InboundMessage:
    return InboundMessage(
        text=text,
        context=context,
        video_sources=(VideoSource(url='https://multimedia.nt.qq.com.cn/download?rkey=x', file='a1b2.mp4'),),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_related_scope_registers_unrelated_group_video_without_model_call(db) -> None:
    """related 下群里无关视频不起模型任务，只登记为未看，正文保持 [视频]。"""
    describer = _FakeVideoDescriber()
    chat = ChatService(db, None, None, None, _noop, cfg=_config(), video_describer=describer)
    context = _group_context(chat._registry)

    await chat.send(_video_message(context))

    stream_id = context.stream.id
    assert describer.calls == []
    assert len(chat._video_unwatched[stream_id]) == 1
    buffered = chat._buffers[stream_id][0]
    assert buffered.video_description_task is None
    assert chat.memory.message_content(stream_id, buffered.message_id) == '看看这个[视频]'


@pytest.mark.asyncio
async def test_mention_triggers_catch_up_and_turn_waits_for_it(db) -> None:
    """@ 她的消息进入缓冲时，窗口内还没看的视频一起补看，回合开始前任务已结束。"""
    describer = _FakeVideoDescriber()
    describer.gate.clear()
    chat = ChatService(db, None, None, None, _noop, cfg=_config(), video_describer=describer)
    context = _group_context(chat._registry)
    stream_id = context.stream.id

    await chat.send(_video_message(context))
    video_message_id = chat._buffers[stream_id][0].message_id
    await chat.send(InboundMessage(text='@月璃 你快看', context=context, mentioned_me=True))

    # 补看任务已起但未放行：回合开始前的物化必须等它。
    materialize = asyncio.create_task(chat._materialize_batch_images(chat._buffers[stream_id]))
    await asyncio.sleep(0.05)
    assert not materialize.done()

    describer.gate.set()
    await materialize
    assert describer.calls == [
        (VideoSource(url='https://multimedia.nt.qq.com.cn/download?rkey=x', file='a1b2.mp4'),),
    ]
    assert chat.memory.message_content(stream_id, video_message_id) == \
        '看看这个[视频：两个人在打射击游戏，配音在说别抓我]'
    assert stream_id not in chat._video_catchup_tasks


@pytest.mark.asyncio
async def test_catch_up_skips_videos_outside_working_memory_window(db) -> None:
    """补看只覆盖她工作记忆窗口内的视频；窗口外的不补也不删登记。"""
    describer = _FakeVideoDescriber()
    config = _config()
    config.conversation.working_memory_messages = 2
    chat = ChatService(db, None, None, None, _noop, cfg=config, video_describer=describer)
    context = _group_context(chat._registry)
    stream_id = context.stream.id

    await chat.send(_video_message(context))
    await chat.send(InboundMessage(text=' filler 一', context=context))
    await chat.send(InboundMessage(text=' filler 二', context=context))
    await chat.send(InboundMessage(text='@月璃 你快看', context=context, mentioned_me=True))
    await asyncio.sleep(0.05)

    assert describer.calls == []
    assert len(chat._video_unwatched[stream_id]) == 1


@pytest.mark.asyncio
async def test_all_scope_watches_on_arrival(db) -> None:
    """all 下名单内会话的视频到了就看，与是否跟她有关无关。"""
    describer = _FakeVideoDescriber()
    chat = ChatService(
        db, None, None, None, _noop,
        cfg=_config(chat_video_scope='all'),
        video_describer=describer,
    )
    context = _group_context(chat._registry)

    await chat.send(_video_message(context))

    stream_id = context.stream.id
    buffered = chat._buffers[stream_id][0]
    assert buffered.video_description_task is not None
    await buffered.video_description_task
    assert len(describer.calls) == 1
    assert chat.memory.message_content(stream_id, buffered.message_id) == \
        '看看这个[视频：两个人在打射击游戏，配音在说别抓我]'


@pytest.mark.asyncio
async def test_disabled_switch_never_watches_nor_registers(db) -> None:
    """开关关闭时一次都不调用，也不登记未看。"""
    describer = _FakeVideoDescriber()
    config = _config()
    config.vision.chat_video_enabled = False
    chat = ChatService(db, None, None, None, _noop, cfg=config, video_describer=describer)
    context = _group_context(chat._registry)

    await chat.send(_video_message(context))

    stream_id = context.stream.id
    assert describer.calls == []
    assert not chat._video_unwatched.get(stream_id)
    assert chat._buffers[stream_id][0].video_description_task is None


@pytest.mark.asyncio
async def test_direct_video_is_watched_under_related_scope(db) -> None:
    """私聊视频在 related 下直接看：私聊本身就是「跟她有关」。"""
    describer = _FakeVideoDescriber()
    chat = ChatService(db, None, None, None, _noop, cfg=_config(), video_describer=describer)
    context = _direct_context(chat._registry)

    await chat.send(_video_message(context))

    stream_id = context.stream.id
    buffered = chat._buffers[stream_id][0]
    assert buffered.video_description_task is not None
    await buffered.video_description_task
    assert len(describer.calls) == 1


@pytest.mark.asyncio
async def test_silent_path_related_video_is_watched(db) -> None:
    """静默路径（门控丢弃但落库的消息）同样按 D6 决定：相关的立即看。"""
    describer = _FakeVideoDescriber()
    chat = ChatService(db, None, None, None, _noop, cfg=_config(), video_describer=describer)
    context = _group_context(chat._registry)

    await chat.record_silent_inbound(_video_message(context, mentioned_me=True), 'attention_filtered')
    await asyncio.sleep(0.05)

    assert len(describer.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['image', 'video'])
async def test_image_and_video_merge_into_same_body_regardless_of_order(db, first: str) -> None:
    """同一条消息既有图片又有视频：两种完成顺序下最终正文与库里正文都同时含两种描述。"""
    image_describer = _GatedImageDescriber()
    video_describer = _FakeVideoDescriber()
    video_describer.gate.clear()
    import base64

    chat = ChatService(
        db, None, None, None, _noop, cfg=_config(),
        image_describer=image_describer, video_describer=video_describer,
    )
    context = _group_context(chat._registry)
    stream_id = context.stream.id

    await chat.send(InboundMessage(
        text='[图片][视频]',
        context=context,
        mentioned_me=True,
        image_sources=('base64://' + base64.b64encode(b'img').decode('ascii'),),
        video_sources=(VideoSource(url='https://multimedia.nt.qq.com.cn/download?rkey=x', file='a1b2.mp4'),),
    ))
    buffered = chat._buffers[stream_id][0]
    assert buffered.image_description_task is not None
    assert buffered.video_description_task is not None

    if first == 'image':
        image_describer.gate.set()
        await buffered.image_description_task
        assert chat.memory.message_content(stream_id, buffered.message_id) == \
            '[图片：一只猫][视频]'
        video_describer.gate.set()
        await buffered.video_description_task
    else:
        video_describer.gate.set()
        await buffered.video_description_task
        assert chat.memory.message_content(stream_id, buffered.message_id) == \
            '[图片][视频：两个人在打射击游戏，配音在说别抓我]'
        image_describer.gate.set()
        await buffered.image_description_task

    expected = '[图片：一只猫][视频：两个人在打射击游戏，配音在说别抓我]'
    assert chat.memory.message_content(stream_id, buffered.message_id) == expected
    materialized = await chat._materialize_batch_images(chat._buffers[stream_id])
    assert materialized[0].text == expected


@pytest.mark.asyncio
async def test_emoji_registration_await_does_not_clobber_video_description(db) -> None:
    """表情包入库的 await 之后写回时，必须以库里当前正文为底，不能盖掉视频描述。

    覆盖的交错：图片任务读出正文 → 表情包入库（await）→ 视频任务在此期间写库
    → 图片任务写回。写回用的底必须是入库之后重新读到的正文。
    """
    import base64

    from src.core.services.media.chat_image import DescribedEmoji

    class _GatedEmojiLibrary:
        def __init__(self) -> None:
            self.gate = asyncio.Event()
            self.register_started = asyncio.Event()
            self.registered: list[str] = []

        async def register(
            self,
            image_bytes: bytes,
            emotion_tags: Any,
            media_type: str,
            content_hash: str,
            sub_type: int,
        ) -> None:
            self.register_started.set()
            await self.gate.wait()
            self.registered.append(content_hash)

    class _EmojiOnlyImageDescriber:
        async def describe_sources(self, sources: tuple[str, ...]) -> list[Any]:
            return [None] * len(sources)

        async def describe_emoji_sources(self, sources: tuple[str, ...]) -> list[Any]:
            return [DescribedEmoji(
                content_hash='deadbeef',
                emotion_tags='笑死',
                image_bytes=b'emoji-bytes',
                media_type='image/jpeg',
            )]

    emoji_library = _GatedEmojiLibrary()
    video_describer = _FakeVideoDescriber()
    video_describer.gate.clear()
    config = _config()
    config.emoji.collect_enabled = True
    chat = ChatService(
        db, None, None, None, _noop, cfg=config,
        image_describer=_EmojiOnlyImageDescriber(),
        video_describer=video_describer,
        emoji_library=emoji_library,
    )
    context = _group_context(chat._registry)
    stream_id = context.stream.id

    await chat.send(InboundMessage(
        text='[表情包][视频]',
        context=context,
        mentioned_me=True,
        emoji_sources=('base64://' + base64.b64encode(b'emoji').decode('ascii'),),
        emoji_sub_types=(1,),
        video_sources=(VideoSource(url='https://multimedia.nt.qq.com.cn/download?rkey=x', file='a1b2.mp4'),),
    ))
    buffered = chat._buffers[stream_id][0]
    # 等图片任务读到旧正文并挂在入库闸门上，再放行视频任务写库，最后放行入库。
    await emoji_library.register_started.wait()
    video_describer.gate.set()
    await buffered.video_description_task
    assert chat.memory.message_content(stream_id, buffered.message_id) ==         '[表情包][视频：两个人在打射击游戏，配音在说别抓我]'
    emoji_library.gate.set()
    await buffered.image_description_task

    final = chat.memory.message_content(stream_id, buffered.message_id)
    assert '[表情包：笑死]' in final
    assert '[视频：两个人在打射击游戏，配音在说别抓我]' in final
