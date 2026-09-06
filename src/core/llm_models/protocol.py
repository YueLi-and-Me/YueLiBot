"""定义模型提供方的异步流式调用协议。

业务服务只依赖 `LlmProvider.stream` 的参数和增量字典结构，不直接依赖具体厂商
客户端，从而可以在测试中注入替身 provider。

增量字典的语义判据也放在这里：``is_committing_chunk`` 定义哪些字段一旦交给调用
方就不可重放，供 provider 内部重试与候选路由共用同一份标准。
"""

from __future__ import annotations

from typing import AsyncIterator, Callable, Protocol

import asyncio


ResponseValidator = Callable[[str], None]


def is_committing_chunk(chunk: dict) -> bool:
    """判断一个增量是否已构成不可重放的对外输出。

    重试与候选切换用它取代「产生过任何增量」这一过宽的判据：

    - 现象：候选先返回一段 ``reasoning``、随后流中途断开时，路由层按「已经开口」
      处理，直接终局，配置好的备用候选一个都不尝试。
    - 原因：``reasoning`` 只进观测面板，回复生成与决策链路都按空文本跳过它，
      既不触发协议解析也不放行事件；把它计入已产出内容等于把无副作用的失败
      误判成不可挽回。
    - 后果：判据放宽到全部字段会让正文重放，同一句话说两遍、事件与工具副作用
      各生效一次，因此只有 ``text`` 与 ``tool_calls`` 才封锁重试。

    :param chunk: provider 产出的增量字典。
    :return: 携带非空 ``text`` 或非空 ``tool_calls`` 时为 ``True``。
    """
    return bool(chunk.get('text')) or bool(chunk.get('tool_calls'))


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
