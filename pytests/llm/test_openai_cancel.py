"""OpenAI 兼容流在网络等待和重试期间的取消边界。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List
import asyncio

import pytest

from src.core.llm_models import openai as openai_module
from src.core.llm_models.openai import LlmError, OpenAiChatProvider


_FIXTURE_API_KEY = '-'.join(('test', 'key'))
_LINES = [
    'data: {"choices":[{"delta":{"content":"甲"}}]}',
    'data: {"choices":[{"delta":{"content":"乙"}}]}',
    'data: [DONE]',
]


class _FakeResponse:
    """在指定行前挂起，关闭状态由测试直接观察。"""

    def __init__(
        self,
        *,
        pause_before: int | None = None,
        pause_enter: bool = False,
        pause_body: bool = False,
    ) -> None:
        self.status_code = 500 if pause_body else 200
        self.pause_before = pause_before
        self.pause_enter = pause_enter
        self.pause_body = pause_body
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.client_closed = asyncio.Event()

    async def __aenter__(self) -> '_FakeResponse':
        if self.pause_enter:
            self.waiting.set()
            await self.release.wait()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.closed.set()

    async def aread(self) -> bytes:
        if self.pause_body:
            self.waiting.set()
            await self.release.wait()
        return b'gateway error'

    async def aiter_lines(self) -> AsyncIterator[str]:
        for index, line in enumerate(_LINES):
            if index == self.pause_before:
                self.waiting.set()
                await self.release.wait()
            yield line


def _install_response(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse) -> None:
    """使用纯内存假连接，保证用例不会调用真实模型。"""

    class _Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> '_Client':
            return self

        async def __aexit__(self, *_args: Any) -> None:
            response.client_closed.set()

        def stream(self, *_args: Any, **_kwargs: Any) -> _FakeResponse:
            return response

    monkeypatch.setattr(openai_module.httpx, 'AsyncClient', _Client)


def _provider(*, retry_interval_ms: int = 0) -> OpenAiChatProvider:
    return OpenAiChatProvider(
        base_url='https://example.invalid/v1',
        api_key=_FIXTURE_API_KEY,
        model='test-model',
        max_retries=1,
        retry_interval_ms=retry_interval_ms,
    )


async def test_cancel_before_first_line_closes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首行未到时置位取消，半秒内上抛 aborted 并关闭连接。"""
    response = _FakeResponse(pause_before=0)
    _install_response(monkeypatch, response)
    signal = asyncio.Event()
    task = asyncio.create_task(_collect(_provider(), signal))

    await asyncio.wait_for(response.waiting.wait(), 1)
    signal.set()
    with pytest.raises(LlmError) as caught:
        await asyncio.wait_for(task, 0.5)

    assert caught.value.kind == 'aborted'
    assert response.closed.is_set()


async def test_cancel_during_stream_closes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已经读到正文后，下一行挂起也能及时取消。"""
    response = _FakeResponse(pause_before=1)
    _install_response(monkeypatch, response)
    signal = asyncio.Event()
    stream = _provider().stream(messages=[], signal=signal)

    assert await anext(stream) == {'text': '甲'}
    task = asyncio.create_task(anext(stream))
    await asyncio.wait_for(response.waiting.wait(), 1)
    signal.set()
    with pytest.raises(LlmError) as caught:
        await asyncio.wait_for(task, 0.5)

    assert caught.value.kind == 'aborted'
    assert response.closed.is_set()


@pytest.mark.parametrize('phase', ['enter', 'error_body'])
async def test_cancel_other_network_waits(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """等响应头及错误正文时也须及时取消并关闭已建立的连接。"""
    response = _FakeResponse(
        pause_enter=phase == 'enter',
        pause_body=phase == 'error_body',
    )
    _install_response(monkeypatch, response)
    signal = asyncio.Event()
    task = asyncio.create_task(_collect(_provider(), signal))

    await asyncio.wait_for(response.waiting.wait(), 1)
    signal.set()
    with pytest.raises(LlmError) as caught:
        await asyncio.wait_for(task, 0.5)

    assert caught.value.kind == 'aborted'
    assert response.client_closed.is_set()
    if phase == 'error_body':
        assert response.closed.is_set()


class _RetryProvider(OpenAiChatProvider):
    """第一次请求在输出前失败，用来固定重试间隔的等待阶段。"""

    def __init__(self) -> None:
        super().__init__(
            base_url='https://example.invalid/v1',
            api_key=_FIXTURE_API_KEY,
            model='test-model',
            max_retries=1,
            retry_interval_ms=1_000,
        )
        self.failed = asyncio.Event()
        self.attempts = 0

    async def _stream_once(
        self,
        messages: List[Dict],
        temperature: float,
        max_tokens: int | None,
        signal: asyncio.Event | None,
        tools: List[Dict] | None = None,
    ) -> AsyncIterator[Dict]:
        self.attempts += 1
        self.failed.set()
        raise LlmError('network', '假连接失败')
        if False:
            yield {}


class _ObservedSignal(asyncio.Event):
    """标记客户端何时开始等取消信号，以确认测试确实进入重试间隔。"""

    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def wait(self) -> bool:
        self.waiting.set()
        return await super().wait()


async def test_cancel_during_retry_interval() -> None:
    """重试睡眠不应拖延取消，也不应发起第二次请求。"""
    provider = _RetryProvider()
    signal = _ObservedSignal()
    task = asyncio.create_task(_collect(provider, signal))

    await asyncio.wait_for(provider.failed.wait(), 1)
    signal.waiting.clear()
    await asyncio.wait_for(signal.waiting.wait(), 1)
    signal.set()
    with pytest.raises(LlmError) as caught:
        await asyncio.wait_for(task, 0.5)

    assert caught.value.kind == 'aborted'
    assert provider.attempts == 1


async def test_uncancelled_stream_keeps_exact_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消信号未置位时，正文分片和顺序保持现状。"""
    response = _FakeResponse()
    _install_response(monkeypatch, response)

    assert await _collect(_provider(), asyncio.Event()) == [
        {'text': '甲'},
        {'text': '乙'},
    ]
    assert response.closed.is_set()


async def _collect(
    provider: OpenAiChatProvider,
    signal: asyncio.Event,
) -> List[Dict]:
    return [chunk async for chunk in provider.stream(messages=[], signal=signal)]
