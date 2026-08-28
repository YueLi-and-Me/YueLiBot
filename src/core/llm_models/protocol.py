"""定义模型提供方的异步流式调用协议。

业务服务只依赖 `LlmProvider.stream` 的参数和增量字典结构，不直接依赖具体厂商
客户端，从而可以在测试中注入替身 provider。
"""

from __future__ import annotations

from typing import AsyncIterator, Callable, Protocol

import asyncio


ResponseValidator = Callable[[str], None]


class LlmProvider(Protocol):
    """模型流式调用协议。"""

    def stream(
        self,
        messages: list[dict],
        temperature: float = ...,
        max_tokens: int | None = ...,
        signal: asyncio.Event | None = ...,
        response_format: dict[str, str] | None = ...,
        response_validator: ResponseValidator | None = ...,
    ) -> AsyncIterator[dict]:
        """按消息上下文产生模型输出增量。

        :param messages: OpenAI 风格的角色/内容消息列表。
        :param temperature: 可选采样温度。
        :param max_tokens: 可选最大输出 token 数。
        :param signal: 可选取消事件。
        :param response_format: 可选结构化响应格式。
        :param response_validator: 可选完整正文校验器；返回前校验，失败时抛出
            ``ValueError``。
        :return: 异步迭代器；每项通常包含 `text`、`reasoning` 或其他 provider 约定字段。
        :raises Exception: 具体实现可抛出网络、鉴权、限流和取消异常。
        副作用：具体实现通常会发起模型网络请求。
        """
        ...
