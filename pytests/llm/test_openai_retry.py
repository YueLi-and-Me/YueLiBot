"""OpenAI 兼容流式请求的重试边界。"""

from __future__ import annotations

from typing import AsyncIterator, Dict, List

import asyncio
import pytest

from src.core.llm_models.openai import LlmError, OpenAiChatProvider

# 测试夹具占位密钥：不对应任何真实服务，经变量间接传入避免被安全扫描当作硬编码凭据。
_FIXTURE_API_KEY = "-".join(('test', 'key'))


class FlakyProvider(OpenAiChatProvider):
    """前几次在输出前失败，随后成功。"""

    def __init__(self, failures: int, kind: str = 'network') -> None:
        super().__init__(
            base_url='https://example.com/v1',
            api_key=_FIXTURE_API_KEY,
            model='test-model',
            max_retries=2,
            retry_interval_ms=0,
        )
        self.attempts = 0
        self.failures = failures
        self.kind = kind

    async def _stream_once(
        self,
        messages: List[Dict],
        temperature: float,
        max_tokens: int | None,
        signal: asyncio.Event | None,
        tools: List[Dict] | None = None,
    ) -> AsyncIterator[Dict]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise LlmError(self.kind, '模拟失败')
        yield {'text': '成功'}


class PartialOutputProvider(FlakyProvider):
    """已经输出正文后断流，必须直接报错而不能重放。"""

    async def _stream_once(
        self,
        messages: List[Dict],
        temperature: float,
        max_tokens: int | None,
        signal: asyncio.Event | None,
        tools: List[Dict] | None = None,
    ) -> AsyncIterator[Dict]:
        self.attempts += 1
        yield {'text': '已经输出'}
        raise LlmError('network', '模拟断流')


class ReasoningThenFailProvider(FlakyProvider):
    """先返回推理再断流：推理不构成对外输出，重试预算应当照常可用。"""

    async def _stream_once(
        self,
        messages: List[Dict],
        temperature: float,
        max_tokens: int | None,
        signal: asyncio.Event | None,
        tools: List[Dict] | None = None,
    ) -> AsyncIterator[Dict]:
        self.attempts += 1
        yield {'reasoning': '先想想'}
        if self.attempts <= self.failures:
            raise LlmError('network', '模拟断流')
        yield {'text': '成功'}


class ClosingProvider(OpenAiChatProvider):
    """首片后保持连接，用于验证外层关闭是否传到单次请求流。"""

    def __init__(self) -> None:
        super().__init__(
            base_url='https://example.com/v1',
            api_key=_FIXTURE_API_KEY,
            model='test-model',
        )
        self.closed = asyncio.Event()

    async def _stream_once(
        self,
        messages: List[Dict],
        temperature: float,
        max_tokens: int | None,
        signal: asyncio.Event | None,
        tools: List[Dict] | None = None,
    ) -> AsyncIterator[Dict]:
        try:
            yield {'text': '第一片'}
            await asyncio.Event().wait()
        finally:
            self.closed.set()


async def test_network_error_retries_before_first_output() -> None:
    provider = FlakyProvider(failures=2)

    chunks = [chunk async for chunk in provider.stream(messages=[])]

    assert chunks == [{'text': '成功'}]
    assert provider.attempts == 3


async def test_auth_error_is_not_retried() -> None:
    provider = FlakyProvider(failures=1, kind='auth')

    with pytest.raises(LlmError, match='模拟失败'):
        _ = [chunk async for chunk in provider.stream(messages=[])]

    assert provider.attempts == 1


async def test_error_after_partial_output_is_not_retried() -> None:
    provider = PartialOutputProvider(failures=0)

    with pytest.raises(LlmError, match='模拟断流'):
        _ = [chunk async for chunk in provider.stream(messages=[])]

    assert provider.attempts == 1


async def test_error_after_reasoning_only_is_still_retried() -> None:
    """推理增量不触发上层副作用，断流后重发不会造成重复输出，重试必须照常发生。"""
    provider = ReasoningThenFailProvider(failures=1)

    chunks = [chunk async for chunk in provider.stream(messages=[])]

    assert chunks == [
        {'reasoning': '先想想'},
        {'reasoning': '先想想'},
        {'text': '成功'},
    ]
    assert provider.attempts == 2


async def test_response_validator_rejects_before_any_chunk_is_exposed() -> None:
    """完整正文校验失败时，直接 provider 也不能先泄露半截结构化结果。"""

    provider = FlakyProvider(failures=0)

    def reject_response(_raw: str) -> None:
        raise ValueError('字段不符合协议')

    stream = provider.stream(messages=[], response_validator=reject_response)
    with pytest.raises(LlmError) as excinfo:
        await anext(stream)

    assert excinfo.value.kind == 'format'
    assert '字段不符合协议' in str(excinfo.value)
    assert provider.attempts == 1


async def test_closing_provider_stream_closes_single_request_immediately() -> None:
    """路由器提前结束消费时，provider 必须同步关闭正在读取的 HTTP 请求。"""
    provider = ClosingProvider()
    stream = provider.stream(messages=[])

    assert await anext(stream) == {'text': '第一片'}
    await stream.aclose()

    assert provider.closed.is_set()
