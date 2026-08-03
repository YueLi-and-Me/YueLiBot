"""OpenAI 兼容流式请求的重试边界。"""

from __future__ import annotations

from typing import AsyncIterator, Dict, List

import asyncio
import pytest

from yueli.llm.openai import LlmError, OpenAiChatProvider


class FlakyProvider(OpenAiChatProvider):
    """前几次在输出前失败，随后成功。"""

    def __init__(self, failures: int, kind: str = 'network') -> None:
        super().__init__(
            base_url='https://example.com/v1',
            api_key='test-key',
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
    ) -> AsyncIterator[Dict]:
        self.attempts += 1
        yield {'text': '已经输出'}
        raise LlmError('network', '模拟断流')


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
