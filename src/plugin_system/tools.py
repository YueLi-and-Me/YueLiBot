"""定义工具插件基类与 ``@tool`` 声明装饰器。

工具插件把「声明」和「实现」写在同一个方法上：装饰器产出 :class:`ToolSpec`，
被装饰的方法本身就是执行体。这样新增一个工具不必再单独造一个实现执行器协议的
对象，也不会出现声明与实现分处两地、改一处忘另一处的情况。

装饰器不是第二个真相来源：它产出的仍是 ToolSpec，注册表与回合帧的过滤规则
一字未变，只是换了书写位置。

依赖 ``plugin`` 基类、``src.core.tooling`` 的声明与执行协议；被具体工具插件继承，
被宿主收集后登记进 ToolRegistry。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Tuple

import inspect

from src.core.tooling.spec import (
    DEFAULT_TOOL_TIMEOUT_MS,
    ToolContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolSideEffect,
    ToolSpec,
)

from .plugin import Plugin


# 被 @tool 装饰的方法上挂载声明的属性名。用双下划线前缀避免与插件自己的属性撞名。
_TOOL_SPEC_ATTR = '__yueli_tool_spec__'

# 工具执行体的签名：一次调用进，一个结果出，与 ToolExecutor 协议逐字对应。
ToolHandler = Callable[[ToolInvocation, ToolContext], Any]


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


class ToolPlugin(Plugin):
    """贡献外部工具的插件基类。

    三个挂载点，后两个有默认实现、按需覆写：

    - :meth:`tools` 贡献工具声明与执行体，由 ``@tool`` 装饰器自动收集；
    - :meth:`observe_inbound` 观察入站消息，供需要会话状态的工具建缓存；
    - :meth:`stream_capabilities` 按会话贡献平台能力，决定工具在该会话是否可见。
    """

    def tools(self) -> List[Tuple[ToolSpec, _BoundToolExecutor]]:
        """收集本插件用 ``@tool`` 声明的全部工具。

        扫描的是实例而非类，因此拿到的是绑定方法，执行时 ``self`` 已就位。
        子类通常不需要覆写；确有动态生成工具的需要时可以覆写并自行返回。

        **本方法在 ``on_load`` 之前被调用。** 宿主必须先把工具登记进注册表，
        才能把注册表交给对话代理构造，而 ``on_load`` 允许做 I/O、只能在事件循环里
        执行，两者的先后由此固定。因此工具声明不得依赖 ``on_load`` 建立的状态——
        需要按配置决定声明什么的工具，当前这套装配顺序还支持不了。

        :return: ``(声明, 执行器)`` 列表，按工具名排序保证登记顺序可复现。
        :raises ValueError: 同一插件内两个方法声明了同名工具——跨插件重名由注册表
            拒绝，插件内重名在这里就该发现。
        """
        collected: Dict[str, Tuple[ToolSpec, _BoundToolExecutor]] = {}
        for _name, member in inspect.getmembers(self, inspect.ismethod):
            spec = getattr(member.__func__, _TOOL_SPEC_ATTR, None)
            if spec is None:
                continue
            if spec.name in collected:
                raise ValueError(
                    f'插件 {self.manifest.plugin_id} 内重复声明了工具 {spec.name}'
                )
            collected[spec.name] = (spec, _BoundToolExecutor(member))
        return [collected[key] for key in sorted(collected)]

    def observe_inbound(
        self,
        stream_id: int,
        message_id: int,
        inbound: Any,
    ) -> None:
        """观察一条已入库的入站消息。

        默认空实现：多数工具无状态。需要会话缓存的工具（例如按路径浏览合并转发）
        在这里建立自己的索引。

        :param stream_id: 会话编号。
        :param message_id: 该消息在主体侧的内部编号。
        :param inbound: 入站消息对象；字段见 ``src.core.platform_io.types``。
        :return: ``None``。
        副作用：由具体实现决定；不得阻塞入站路径，也不得抛异常。
        """

    def stream_capabilities(self, stream_id: int) -> FrozenSet[str]:
        """返回本插件为该会话贡献的平台能力。

        用于表达「这个工具在这个会话里有没有东西可读」——例如合并转发工具只在
        缓存里确实有转发内容的会话才该出现在工具声明里。

        :param stream_id: 会话编号。
        :return: 能力名集合；默认空集合，表示不贡献任何能力。
        """
        return frozenset()
