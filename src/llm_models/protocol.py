"""模型提供方的调用契约。"""

from __future__ import annotations

from typing import AsyncIterator, Protocol

import asyncio

class LlmProvider(Protocol):
    """模型流式调用协议。"""

    def stream(
        self,
        messages: list[dict],
        temperature: float = ...,
        max_tokens: int | None = ...,
        signal: asyncio.Event | None = ...,
        response_format: dict[str, str] | None = ...,
    ) -> AsyncIterator[dict]:
        ...
