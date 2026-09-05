"""聊天图片描述服务与占位符合并回归。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator
import asyncio

import httpx
import pytest

from src.core.config.schema import Config
from src.core.services.media.chat_image import (
    ChatImageDescriber,
    _read_image_source,
    merge_image_descriptions,
)


class _FakeVision:
    """按脚本返回图片描述的视觉模型替身。"""

    model = 'vision-test'

    def __init__(self, scripts: list[str]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    async def stream(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        self.requests.append({'messages': messages, **kwargs})
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        yield {'text': script}


class _HangingVision:
    """先返回推理增量再持续等待，并记录图片截止是否关闭了流。"""

    model = 'vision-hanging'

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def stream(
        self,
        messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        try:
            yield {'reasoning': '还在分析图片'}
            await asyncio.Event().wait()
        finally:
            self.closed.set()


def _config() -> Config:
    config = Config()
    config.vision.chat_image_enabled = True
    return config


def test_merge_image_descriptions_keeps_failure_placeholder_in_order() -> None:
    assert merge_image_descriptions('看[图片]和[图片]', [None, '一只猫']) == \
        '看[图片]和[图片：一只猫]'
    assert merge_image_descriptions('看[图片]', ['一只猫']) == '看[图片：一只猫]'
    assert merge_image_descriptions('看[表情包]', ['不应替换']) == '看[表情包]'


@pytest.mark.asyncio
async def test_image_describer_caches_by_content_hash() -> None:
    provider = _FakeVision(['一只猫'])
    describer = ChatImageDescriber(_config(), provider)

    first = await describer.describe(b'image-bytes')
    second = await describer.describe(b'image-bytes')

    assert first == '一只猫'
    assert second == '一只猫'
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_image_describer_empty_response_returns_none() -> None:
    provider = _FakeVision([''])
    describer = ChatImageDescriber(_config(), provider)

    assert await describer.describe(b'image-bytes') is None
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_image_describer_requires_visible_text_from_router() -> None:
    """图片描述必须显式要求正文，不能让空流或纯推理响应占用成功候选。"""
    provider = _FakeVision(['一只猫'])
    describer = ChatImageDescriber(_config(), provider)

    assert await describer.describe(b'image-bytes') == '一只猫'
    assert provider.requests[0]['require_text'] is True


@pytest.mark.asyncio
async def test_image_deadline_closes_hanging_model_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """图片总截止触发后必须先关闭模型流，不能交给异步生成器 GC 收尾。"""
    provider = _HangingVision()
    describer = ChatImageDescriber(_config(), provider)
    monkeypatch.setattr(describer, '_description_deadline_s', 0.01)

    assert await describer.describe(b'image-bytes') is None
    await asyncio.wait_for(provider.closed.wait(), timeout=0.2)

    assert provider.closed.is_set()


def test_image_deadline_covers_configured_first_token_window() -> None:
    """图片总截止不得先于视觉 Router 的首字超时取消请求。"""
    config = _config()
    config.routing.vision.first_token_timeout_ms = 30_000

    describer = ChatImageDescriber(config, _FakeVision(['一只猫']))

    assert describer._description_deadline_s == 35.0


@pytest.mark.asyncio
async def test_describe_sources_downloads_and_aligns_results() -> None:
    """来源引用由服务在后台并发解析，结果顺序与占位符一致。"""
    import base64

    provider = _FakeVision(['一只猫', '第二张图'])
    describer = ChatImageDescriber(_config(), provider)

    results = await describer.describe_sources([
        'base64://' + base64.b64encode(b'one').decode('ascii'),
        '',
        'base64://' + base64.b64encode(b'two').decode('ascii'),
    ])

    assert results[0] == '一只猫'
    assert results[1] is None
    assert results[2] == '第二张图'


@pytest.mark.asyncio
async def test_read_image_source_sends_referer_for_qpic_cdn() -> None:
    """QQ 图片 CDN 需要同域 Referer 才允许下载。"""
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        if request.headers.get('Referer') != 'https://gchat.qpic.cn/':
            return httpx.Response(400, text='{"retcode":-5503007}')
        return httpx.Response(200, content=b'png-bytes')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        content = await _read_image_source(
            'https://gchat.qpic.cn/download?appid=1406&fileid=test&rkey=test',
            client,
        )
    finally:
        await client.aclose()

    assert content == b'png-bytes'
    assert seen_headers.get('referer') == 'https://gchat.qpic.cn/'
    assert 'Mozilla/5.0' in seen_headers.get('user-agent', '')


@pytest.mark.asyncio
async def test_read_image_source_decodes_percent_escaped_file_uri(tmp_path: Path) -> None:
    """Path.as_uri 生成的空间编码路径必须能被本地图片读取识别。"""
    directory = tmp_path / 'Tencent Files'
    directory.mkdir()
    image_path = directory / 'a b.jpg'
    image_path.write_bytes(b'jpg-bytes')

    assert await _read_image_source(image_path.as_uri()) == b'jpg-bytes'


@pytest.mark.asyncio
async def test_read_image_source_keeps_other_hosts_without_referer() -> None:
    """非 QQ CDN 图片来源保持原请求行为，不额外注入请求头。"""
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        return httpx.Response(200, content=b'generic-bytes')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        content = await _read_image_source('https://example.com/a.png', client)
    finally:
        await client.aclose()

    assert content == b'generic-bytes'
    assert 'referer' not in seen_headers


@pytest.mark.asyncio
async def test_describe_attachments_decodes_base64_and_aligns_results() -> None:
    import base64

    provider = _FakeVision(['一只猫', '第二张图'])
    describer = ChatImageDescriber(_config(), provider)

    results = await describer.describe_attachments([
        {'data': base64.b64encode(b'one').decode('ascii')},
        {'data': 'not-base64'},
        {'data': base64.b64encode(b'two').decode('ascii')},
    ])

    assert results[0] == '一只猫'
    assert results[1] is None
    assert results[2] == '第二张图'
