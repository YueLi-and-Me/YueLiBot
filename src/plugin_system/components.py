"""插件组件的声明装饰器：``@tool``、``@inbound_rewrite``、``@inbound_observe``
与 ``@command``。

「插件能贡献什么」全部收在本模块：每类组件一个装饰器，装饰器只把声明挂到方法上，
方法本身保持原样返回。被装饰的方法由 :class:`ToolPlugin` 的收集方法按属性标记
找出，宿主再按组件类型分别取用——声明与基类分开书写，新增一类组件时基类不必
再长出一个可覆写方法。

装饰器不是第二个真相来源：``@tool`` 产出的仍是 ToolSpec，注册表与回合帧的过滤
规则不因书写位置变化；命令声明转交给 ``src.core.commands.registry`` 的
``register_command``，本模块不另建一套注册表。

依赖 ``src.core.tooling`` 的声明协议、``src.core.platform_io.types`` 与
``src.core.commands.registry`` 的类型；被具体工具插件书写，被基类收集。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

import inspect

from src.core.commands.registry import CommandContext
from src.core.platform_io.types import InboundMessage
from src.core.tooling.spec import (
    DEFAULT_TOOL_TIMEOUT_MS,
    ToolContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolSideEffect,
    ToolSpec,
)


# 被装饰的方法上挂载声明的属性名。统一双下划线前缀，避免与插件自己的属性撞名。
_TOOL_SPEC_ATTR = '__yueli_tool_spec__'
_INBOUND_REWRITE_ATTR = '__yueli_inbound_rewrite__'
_INBOUND_OBSERVE_ATTR = '__yueli_inbound_observe__'
_COMMAND_ATTR = '__yueli_command__'

# 工具执行体的签名：一次调用进，一个结果出，与 ToolExecutor 协议逐字对应。
ToolHandler = Callable[[ToolInvocation, ToolContext], Any]

# 入站改写体的签名：收到当前入站消息，返回新正文或 None（不改）。
InboundRewriteHandler = Callable[[InboundMessage], Awaitable[Optional[str]]]

# 入站观察体的签名：只读，返回值被忽略。
InboundObserveHandler = Callable[[int, int, InboundMessage], None]

# 命令执行体的签名：与 src.core.commands.registry 的 CommandHandler 一致，
# 同步与异步都接受，分发侧会归一。
PluginCommandHandler = Callable[[CommandContext], Any]


@dataclass(frozen=True)
class InboundRewriteSpec:
    """一个入站改写组件的声明。

    :ivar order: 执行顺序，升序排列；同序时按插件 id 排序。改写器串行接力，
        顺序不确定意味着同一条消息两次运行得到不同结果，那类问题无法复现。
    """

    order: int = 0


@dataclass(frozen=True)
class CommandDeclaration:
    """一个命令组件的声明，字段与 ``register_command`` 的入参一致。

    :ivar name: 以 ``/`` 开头的命令名，用于帮助页展示与去重。
    :ivar pattern: 对完整去首尾空白文本执行匹配的正则表达式。
    :ivar description: 帮助页与只读 WebUI 中展示的简体中文说明。
    """

    name: str
    pattern: str
    description: str


def tool(
    *,
    name: str,
    description: str,
    parameters: Mapping[str, Any] | None = None,
    side_effect: ToolSideEffect = 'readonly',
    capabilities: Iterable[str] = (),
    timeout_ms: int = DEFAULT_TOOL_TIMEOUT_MS,
) -> Callable[[ToolHandler], ToolHandler]:
    """把一个方法声明为插件提供的外部工具。

    ``kind`` 固定为 ``external`` 而不开放给插件：终局动作与认知动作都是封闭枚举，
    判据只有 ``tool_schema`` 一份，允许插件自称这两类会凭空多出一份动作空间判据。

    :param name: 工具名，模型可见的协议面；与已登记的动作或工具重名会在登记期被拒。
    :param description: 一句话说明，口径是「什么时候用它」，不复述工具名。
    :param parameters: OpenAI 兼容的 JSON Schema 对象参数声明；无参数时省略。
    :param side_effect: 副作用分级；注册表拒绝登记 ``irreversible`` 的工具。
    :param capabilities: 所需平台能力；与回合帧能力不符时该工具不进入声明。
    :param timeout_ms: 单次执行超时，必须大于 0。
    :return: 原方法本身，附带解析好的 :class:`ToolSpec`。
    :raises ValueError: 声明本身非法（空名、非正超时、能力含空串），当场抛出而不是
        拖到登记期——装饰器在导入期执行，错误越早暴露定位越准。
    """
    spec = ToolSpec(
        name=name,
        description=description,
        parameters=dict(parameters or {}),
        kind='external',
        side_effect=side_effect,
        capabilities=frozenset(capabilities),
        timeout_ms=timeout_ms,
    )

    def decorate(handler: ToolHandler) -> ToolHandler:
        """把声明挂到方法上，方法本身原样返回。"""
        if not inspect.iscoroutinefunction(handler):
            raise ValueError(f'工具 {name} 的执行体必须是 async 方法')
        setattr(handler, _TOOL_SPEC_ATTR, spec)
        return handler

    return decorate


class _BoundToolExecutor:
    """把插件的绑定方法包装成满足 ToolExecutor 协议的对象。

    存在的理由：注册表要的是带 ``execute`` 的对象，而装饰器要的是「方法即执行体」
    的书写体验。这层包装只做转发，不加任何行为。
    """

    def __init__(self, handler: ToolHandler) -> None:
        """保存已绑定实例的方法。"""
        self._handler = handler

    async def execute(
        self,
        invocation: ToolInvocation,
        context: ToolContext,
    ) -> ToolExecutionResult:
        """转发给被装饰的方法。"""
        return await self._handler(invocation, context)


def inbound_rewrite(*, order: int = 0) -> Callable[[InboundRewriteHandler], InboundRewriteHandler]:
    """把一个方法声明为入站正文改写组件。

    改写发生在消息落库**之前**：改后的正文进入历史、记忆与摘要。执行体收到整个
    :class:`InboundMessage`，其中 ``text`` 是上一步的正文；返回新正文表示改写，
    返回 ``None`` 表示不改。只允许改正文——签名只返回 ``str``，其它字段对插件
    不可写，避免改写器顺手改掉归属或来源事实。

    :param order: 执行顺序，升序；同序时按插件 id 排序，同插件内按方法名排序。
    :return: 原方法本身，附带 :class:`InboundRewriteSpec`。
    :raises ValueError: 执行体不是 async 方法——分发侧会 await 它，同步实现
        返回的协程对象会被当成正文存库，越早拒绝越好定位。
    """
    spec = InboundRewriteSpec(order=order)

    def decorate(handler: InboundRewriteHandler) -> InboundRewriteHandler:
        """把声明挂到方法上，方法本身原样返回。"""
        if not inspect.iscoroutinefunction(handler):
            raise ValueError('入站改写组件的执行体必须是 async 方法')
        setattr(handler, _INBOUND_REWRITE_ATTR, spec)
        return handler

    return decorate


def inbound_observe() -> Callable[[InboundObserveHandler], InboundObserveHandler]:
    """把一个方法声明为入站观察组件。

    观察发生在消息落库**之后**：执行体拿到落库行一致的 ``message_id``，只读，
    返回值被忽略。一个插件可以声明多个观察器，各自独立隔离——其中一个抛异常
    只跳过那一个，不影响其余观察器，也不影响入站主链路。

    :return: 原方法本身，附带观察标记。
    :raises ValueError: 执行体是 async 方法——观察在入站主链路上同步分发，
        返回的协程对象无人 await，观察会静默丢失。
    """

    def decorate(handler: InboundObserveHandler) -> InboundObserveHandler:
        """把标记挂到方法上，方法本身原样返回。"""
        if inspect.iscoroutinefunction(handler):
            raise ValueError('入站观察组件的执行体必须是同步方法')
        setattr(handler, _INBOUND_OBSERVE_ATTR, True)
        return handler

    return decorate


def command(
    *,
    name: str,
    pattern: str,
    description: str,
) -> Callable[[PluginCommandHandler], PluginCommandHandler]:
    """把一个方法声明为插件提供的命令组件。

    声明在此，注册不在此：宿主在发现阶段把收集到的声明转交给
    ``src.core.commands.registry`` 的 ``register_command``，已关闭的插件不会走到
    这一步，它的命令因此不出现在目录里。本装饰器若在导入期直接注册，首次安装时
    入口模块总会被执行一次（配置生成需要插件类），关闭的插件也会把命令留在全局
    目录里。

    继承命令通道既有的两道门控，插件无法放宽：只对 owner 生效，且整体受
    ``[developer] enabled`` 开关控制。插件命令因此是开发者命令，不是面向所有
    用户的命令。

    :param name: 以 ``/`` 开头、不含空白的命令名；同名命令在本插件内重复声明
        会在收集期报错，跨插件重复会在注册期报错。
    :param pattern: 对完整去首尾空白文本执行匹配的正则表达式。
    :param description: 帮助页与只读 WebUI 中展示的简体中文说明。
    :return: 原方法本身，附带 :class:`CommandDeclaration`。
    """
    declaration = CommandDeclaration(
        name=name,
        pattern=pattern,
        description=description,
    )

    def decorate(handler: PluginCommandHandler) -> PluginCommandHandler:
        """把声明挂到方法上，方法本身原样返回。"""
        setattr(handler, _COMMAND_ATTR, declaration)
        return handler

    return decorate


__all__ = [
    'CommandDeclaration',
    'InboundObserveHandler',
    'InboundRewriteHandler',
    'InboundRewriteSpec',
    'PluginCommandHandler',
    'ToolHandler',
    'command',
    'inbound_observe',
    'inbound_rewrite',
    'tool',
]
