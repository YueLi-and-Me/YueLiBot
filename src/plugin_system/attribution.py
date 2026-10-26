"""记录「当前正在执行的代码属于哪个插件」，供模型用量按来源拆分。

对外接口：

- :class:`PluginOrigin`：插件 id 与是否第三方；
- :func:`current_plugin_origin`：读当前上下文的归属，主程序自己的代码读到 ``None``；
- :func:`attributed_async` / :func:`attributed_callable` / :func:`attributed_tool`：
  把插件的执行体包一层，调用期间设置归属、结束后还原。

归属保存在 ContextVar 里。注册表收集插件组件时统一包装（见 ``registry``），主程序
调用这些执行体的地方不需要知道归属这回事；插件在执行体里 ``asyncio.create_task``
出去的后台任务会复制创建时的上下文，因此同样记在该插件名下。今后新增的组件类型
（包括给插件开放的模型调用接口）也必须经过这一层包装。

本模块不依赖插件系统的其他部分，只被 ``registry`` 与模型用量账本
（``src.core.llm_models.usage``）引用。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

import functools
import inspect


@dataclass(frozen=True)
class PluginOrigin:
    """发起调用的插件身份。

    :ivar plugin_id: 插件清单里的 id。
    :ivar third_party: 来自第三方插件目录时为 ``True``，内置插件为 ``False``。
    """

    plugin_id: str
    third_party: bool


# 当前执行代码的插件归属；默认 None 表示主程序。
#
# 归属跟着「谁创建了任务」走，不跟着「谁在等结果」走：
# - 现象：插件等待的是主程序早先已经发起的同一个任务（例如同一张图已经在识别中）
#   时，这次模型调用记在主程序名下，而不是插件名下。
# - 原因：asyncio 任务在创建时复制当前上下文，此后再由谁 await 都不改变它读到的
#   ContextVar；在途去重让插件直接等主程序那条任务，没有重新发起请求。
# - 后果：这是有意的口径，谁真正发起请求就算谁的。若改成按等待方归属，同一次请求
#   会被主程序和插件各记一次，费用重复计算。
_current_origin: ContextVar[PluginOrigin | None] = ContextVar('plugin_origin', default=None)

_T = TypeVar('_T')


def current_plugin_origin() -> PluginOrigin | None:
    """返回当前上下文的插件归属。

    :return: 插件执行体及其派生任务内为对应的 :class:`PluginOrigin`；主程序自己的
        代码为 ``None``。
    副作用：无。
    """
    return _current_origin.get()


def attributed_async(
    origin: PluginOrigin,
    handler: Callable[..., Awaitable[_T]],
) -> Callable[..., Awaitable[_T]]:
    """把插件的异步执行体包成「执行期间带归属」的同签名协程函数。

    :param origin: 执行体所属插件。
    :param handler: 插件的异步执行体（生命周期钩子、入站改写、工具执行等）。
    :return: 调用时先设置归属、等待原执行体结束后还原的协程函数；参数与返回值
        原样透传，异常原样传播。
    副作用：构造时无；每次调用在当前上下文里临时改写归属。
    """

    @functools.wraps(handler)
    async def run(*args: Any, **kwargs: Any) -> _T:
        token = _current_origin.set(origin)
        try:
            return await handler(*args, **kwargs)
        finally:
            _current_origin.reset(token)

    return run


def attributed_callable(origin: PluginOrigin, handler: Callable[..., Any]) -> Callable[..., Any]:
    """按执行体是否为协程函数选择包装方式，同步执行体保持同步。

    入站观察要求同步执行体，命令执行体同步与异步都接受，包装不能改变这一形状：
    把同步执行体包成协程函数会让分发方拿到一个无人 await 的协程。同步执行体若
    返回了可等待对象，分发方稍后 await 时已离开包装，因此把它也包进归属里。

    :param origin: 执行体所属插件。
    :param handler: 插件的同步或异步执行体。
    :return: 与原执行体同步性一致、调用期间带归属的函数。
    副作用：构造时无；每次调用在当前上下文里临时改写归属。
    """
    if inspect.iscoroutinefunction(handler):
        return attributed_async(origin, handler)

    @functools.wraps(handler)
    def run(*args: Any, **kwargs: Any) -> Any:
        token = _current_origin.set(origin)
        try:
            result = handler(*args, **kwargs)
        finally:
            _current_origin.reset(token)
        if inspect.isawaitable(result):
            return _await_attributed(origin, result)
        return result

    return run


async def _await_attributed(origin: PluginOrigin, awaitable: Awaitable[_T]) -> _T:
    """在插件归属下等待同步执行体返回的可等待对象。

    :param origin: 执行体所属插件。
    :param awaitable: 同步执行体返回的协程或 Future。
    :return: 可等待对象的结果。
    副作用：等待期间在当前上下文里设置归属。
    """
    token = _current_origin.set(origin)
    try:
        return await awaitable
    finally:
        _current_origin.reset(token)


class AttributedToolExecutor:
    """给插件工具执行器套上归属的转发对象，满足 ToolExecutor 协议。

    插件可以覆写 ``tools()`` 返回自定义执行器，因此包装放在注册表收集处，而不是
    装饰器生成的默认执行器里，否则覆写者的执行器会漏掉归属。
    """

    def __init__(self, origin: PluginOrigin, executor: Any) -> None:
        """保存归属与原执行器。

        :param origin: 执行器所属插件。
        :param executor: 带 ``execute(invocation, context)`` 协程方法的原执行器。
        副作用：无。
        """
        self._origin = origin
        self._executor = executor

    async def execute(self, invocation: Any, context: Any) -> Any:
        """在插件归属下转发一次工具调用。

        :param invocation: 工具调用参数，原样转交。
        :param context: 工具执行上下文，原样转交。
        :return: 原执行器的执行结果。
        :raises Exception: 原执行器抛出的任何异常原样传播。
        副作用：执行期间在当前上下文里设置归属。
        """
        token = _current_origin.set(self._origin)
        try:
            return await self._executor.execute(invocation, context)
        finally:
            _current_origin.reset(token)


def attributed_tool(origin: PluginOrigin, executor: Any) -> AttributedToolExecutor:
    """把插件的工具执行器包成带归属的执行器。

    :param origin: 执行器所属插件。
    :param executor: 原执行器。
    :return: 调用 ``execute`` 期间带归属的执行器。
    副作用：无。
    """
    return AttributedToolExecutor(origin, executor)


__all__ = [
    'AttributedToolExecutor',
    'PluginOrigin',
    'attributed_async',
    'attributed_callable',
    'attributed_tool',
    'current_plugin_origin',
]
