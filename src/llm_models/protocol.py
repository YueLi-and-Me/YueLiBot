"""模型提供方的调用契约。"""

from __future__ import annotations

from typing import AsyncIterator, Literal, Protocol

import asyncio

# inherit 沿用模型配置。
ThinkingMode = Literal['inherit', 'disabled', 'enabled', 'auto']


class LlmProvider(Protocol):
    """模型流式调用协议。"""

    def stream(
        self,
        messages: list[dict],
        temperature: float = ...,
        max_tokens: int | None = ...,
        signal: asyncio.Event | None = ...,
        response_format: dict[str, str] | None = ...,
        thinking: ThinkingMode = ...,
    ) -> AsyncIterator[dict]:
        ...
