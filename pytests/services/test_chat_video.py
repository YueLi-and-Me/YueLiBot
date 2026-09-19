"""聊天视频理解服务与占位符合并回归。

时长判定、模型调用、缓存合并与截止语义都按 `file`（内容 MD5）对齐；
模型与 HTTP 一律用替身，不连真实服务。
"""

from __future__ import annotations

from typing import Any, AsyncIterator
import asyncio
import json

import httpx
import pytest

from src.core.config.schema import Config
from src.core.llm_models import openai as openai_module
from src.core.llm_models.openai import OpenAiChatProvider
from src.core.platform_io.types import VideoSource
from src.core.services.media.chat_video import (
    ChatVideoDescriber,
    VideoDurationUnreadableError,
    _VideoTooLong,
    merge_video_descriptions,
    message_concerns_her,
)


class _FakeOmni:
    """按脚本返回视频描述的全模态模型替身，记录每次请求。"""

    model = 'omni-test'

    def __init__(self, scripts: list[str]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    async def stream(
        self,
        messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        self.requests.append({'messages': messages, **kwargs})
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        yield {'text': script}


class _HangingOmni:
    """先返回推理增量再持续等待，并记录视频截止是否关闭了流。"""

    model = 'omni-hanging'

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def stream(
        self,
        messages: list[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        try:
            yield {'reasoning': '还在分析视频'}
            await asyncio.Event().wait()
        finally:
            self.closed.set()


def _config(max_seconds: int = 180) -> Config:
    config = Config()
    config.vision.chat_video_enabled = True
    config.vision.chat_video_max_seconds = max_seconds
    return config


def _source(
    url: str = 'https://multimedia.nt.qq.com.cn/download?rkey=abc123',
    file: str = '0123abcdef.mp4',
) -> VideoSource:
    return VideoSource(url=url, file=file)


def _stub_duration(
    monkeypatch: pytest.MonkeyPatch,
    describer: ChatVideoDescriber,
    seconds: float | None,
) -> None:
    """绕过 HTTP 把时长读取替换为固定结果；``None`` 表示读不出。"""
    async def _fake(url: str) -> float:
        if seconds is None:
            raise VideoDurationUnreadableError('链接已过期')
        return seconds

    monkeypatch.setattr(describer, '_read_duration', _fake)


def test_merge_video_descriptions_keeps_failure_placeholder_in_order() -> None:
    assert merge_video_descriptions('看[视频]和[视频]', [None, '一段描述']) == \
        '看[视频]和[视频：一段描述]'
    assert merge_video_descriptions('看[视频]', ['一段描述']) == '看[视频：一段描述]'
    assert merge_video_descriptions('看[图片]', ['不应替换']) == '看[图片]'


def test_merge_video_descriptions_renders_too_long_facts() -> None:
    """超限占位只交代事实：不足 60 秒写「约 N 秒」，否则按分钟四舍五入。"""
    assert merge_video_descriptions('[视频]', [_VideoTooLong(45)]) == \
        '[视频：约 45 秒，超出观看时长上限，未看]'
    assert merge_video_descriptions('[视频]', [_VideoTooLong(720)]) == \
        '[视频：约 12 分钟，超出观看时长上限，未看]'
    assert merge_video_descriptions('[视频]', [_VideoTooLong(89.6)]) == \
        '[视频：约 1 分钟，超出观看时长上限，未看]'


def test_message_concerns_her_covers_exactly_four_signals() -> None:
    assert message_concerns_her('direct', False, False, False) is True
    assert message_concerns_her('group', True, False, False) is True
    assert message_concerns_her('group', False, True, False) is True
    assert message_concerns_her('group', False, False, True) is True
    assert message_concerns_her('group', False, False, False) is False


@pytest.mark.asyncio
async def test_too_long_video_never_reaches_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超过上限的视频不调模型，结果是带时长的超限事实。"""
    provider = _FakeOmni(['不该出现'])
    describer = ChatVideoDescriber(_config(max_seconds=30), provider)
    _stub_duration(monkeypatch, describer, 45.0)

    outcomes = await describer.describe_sources((_source(),))

    assert provider.calls == 0
    assert outcomes == [_VideoTooLong(45.0)]


@pytest.mark.asyncio
async def test_unreadable_duration_keeps_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读不出时长（链接过期、非 MP4、盒子损坏）不送模型，保留占位。"""
    provider = _FakeOmni(['不该出现'])
    describer = ChatVideoDescriber(_config(), provider)
    _stub_duration(monkeypatch, describer, None)

    outcomes = await describer.describe_sources((_source(),))

    assert provider.calls == 0
    assert outcomes == [None]


@pytest.mark.asyncio
async def test_success_replaces_placeholder_and_sends_qq_link_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """视频块里的 url 必须是 QQ 链接原文，且显式要求正文。"""
    provider = _FakeOmni(['两个人在打射击游戏，配音在说别抓我'])
    describer = ChatVideoDescriber(_config(), provider)
    _stub_duration(monkeypatch, describer, 17.5)
    source = _source()

    outcomes = await describer.describe_sources((source,))

    assert outcomes == ['两个人在打射击游戏，配音在说别抓我']
    request = provider.requests[0]
    assert request['require_text'] is True
    content = request['messages'][0]['content']
    assert content[0] == {'type': 'video_url', 'video_url': {'url': source.url}}
    assert content[1]['type'] == 'text'
    assert merge_video_descriptions('看这个[视频]', outcomes) == \
        '看这个[视频：两个人在打射击游戏，配音在说别抓我]'


@pytest.mark.asyncio
async def test_same_file_concurrent_describe_calls_model_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一个视频被转发多次只看一次：在途任务按 file 合并。"""
    provider = _FakeOmni(['同一段描述'])
    describer = ChatVideoDescriber(_config(), provider)
    _stub_duration(monkeypatch, describer, 17.5)

    first, second = await asyncio.gather(
        describer.describe_sources((_source(),)),
        describer.describe_sources((_source(url='https://multimedia.nt.qq.com.cn/download?rkey=other'),)),
    )

    assert first == second == ['同一段描述']
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_timeout_keeps_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总截止触发后先关闭模型流，再保留占位，不让模型猜内容。"""
    provider = _HangingOmni()
    describer = ChatVideoDescriber(_config(), provider)
    _stub_duration(monkeypatch, describer, 17.5)
    monkeypatch.setattr(describer, '_description_deadline_s', 0.01)

    assert await describer.describe_sources((_source(),)) == [None]
    await asyncio.wait_for(provider.closed.wait(), timeout=0.2)

    assert provider.closed.is_set()


@pytest.mark.asyncio
async def test_disabled_switch_never_calls_anything() -> None:
    config = _config()
    config.vision.chat_video_enabled = False
    provider = _FakeOmni(['不该出现'])
    describer = ChatVideoDescriber(config, provider)

    assert await describer.describe_sources((_source(),)) == [None]
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_openai_client_passes_video_block_and_extra_body_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """video_url 块与 extra_body 原样进入请求体，客户端不需要为视频改任何组装。"""
    seen: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen['body'] = json.loads(request.content)
        delta = {'choices': [{'delta': {'content': '一段描述'}}]}
        return httpx.Response(200, text='data: ' + json.dumps(delta) + '\n\ndata: [DONE]\n\n')

    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        openai_module.httpx, 'AsyncClient',
        lambda **kwargs: client_class(transport=transport, **kwargs),
    )
    provider = OpenAiChatProvider(
        base_url='https://dashscope.example/v1',
        api_key='fixture-key',
        model='qwen-omni-test',
        extra_body={'modalities': ['text'], 'reasoning_effort': 'medium'},
    )
    qq_link = 'https://multimedia.nt.qq.com.cn/download?foo=bar&rkey=abc123'
    chunks = [
        chunk async for chunk in provider.stream(
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'video_url', 'video_url': {'url': qq_link}},
                    {'type': 'text', 'text': '描述这段视频'},
                ],
            }],
        )
    ]

    assert [chunk.get('text') for chunk in chunks] == ['一段描述']
    body = seen['body']
    assert body['messages'][0]['content'][0] == {'type': 'video_url', 'video_url': {'url': qq_link}}
    assert body['modalities'] == ['text']
    assert body['reasoning_effort'] == 'medium'
    assert 'max_tokens' not in body


@pytest.mark.asyncio
async def test_trace_events_never_carry_the_signed_qq_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """llm_request 与 video_description 事件的序列化结果里都不含视频链接本身。

    QQ 视频链接带一次性签名，有效期内谁拿到都能下载那段视频；追踪事件要落库并
    显示在 WebUI，与图片服务同口径只记内容标识（file），不记来源。
    """
    from src.core.observe.store import event_store

    provider = _FakeOmni(['一段描述'])
    describer = ChatVideoDescriber(_config(), provider)
    _stub_duration(monkeypatch, describer, 17.5)
    source = _source()

    await describer.describe_sources((source,))

    for kind in ('llm_request', 'video_description'):
        for entry in event_store.search(kinds=[kind]).events:
            assert source.url not in json.dumps(entry, ensure_ascii=False)


@pytest.mark.asyncio
async def test_duration_read_failure_message_does_not_carry_url() -> None:
    """读时长失败的原因文本不含链接本身，追踪与日志可以原样记录它。"""
    from src.core.services.media.chat_video import read_mp4_duration_seconds

    def refused(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('connection refused')

    url = 'https://multimedia.nt.qq.com.cn/download?rkey=secret-link'
    async with httpx.AsyncClient(transport=httpx.MockTransport(refused)) as http:
        with pytest.raises(VideoDurationUnreadableError) as caught:
            await read_mp4_duration_seconds(url, http)

    assert url not in str(caught.value)
