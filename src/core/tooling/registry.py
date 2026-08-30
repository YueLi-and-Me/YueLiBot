"""统一工具注册表。

职责：
1. 登记动作（封闭动作枚举）与外部工具，重名直接拒绝——工具名是模型可见的
   协议面，冲突必须当场暴露而不是静默覆盖；
2. 认知动作绑定工具执行器：终局动作的执行继续走 ConversationAgent 的动作头
   结算，认知动作统一经 ToolExecutor 执行；
3. 按回合帧生成模型可见的工具声明：动作声明与 ``src.core.agent.tool_schema``
   同源生成，外部工具声明由 ToolSpec 派生并按帧能力过滤；
4. 按工具名解析登记项，供执行阶段区分动作路径与工具路径。

依赖：``src.core.agent.action_protocol``（动作枚举与回合帧）、
``src.core.agent.tool_schema``（动作声明同源生成）、``spec`` 与
``executor``；被 ``src.core.services.chat`` 装配并注入
ConversationAgent，不反向依赖聊天服务或模型层。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

from src.core.agent.action_protocol import (
    COGNITIVE_ACTIONS,
    TERMINAL_ACTIONS,
    ConversationAction,
    DecisionFrame,
)
from src.core.agent.tool_schema import (
    build_tool_definitions as build_action_tool_definitions,
)

from .executor import ToolExecutor
from .spec import ToolSpec


@dataclass(frozen=True)
class RegisteredTool:
    """一条已登记的外部工具及其执行器。"""

    spec: ToolSpec
    executor: ToolExecutor


@dataclass(frozen=True)
class ResolvedTool:
    """按工具名解析出的登记项。

    kind 为 action 时 spec 与 executor 为空：动作的声明按帧动态生成、执行走
    动作头结算路径；kind 为 tool 时两者齐备。

    :raises ValueError: kind 为 tool 时缺少声明或执行器。
    """

    kind: Literal['action', 'tool']
    name: str
    spec: Optional[ToolSpec] = None
    executor: Optional[ToolExecutor] = None

    def __post_init__(self) -> None:
        """拒绝声明与执行器缺位的外部工具登记项。"""
        if self.kind == 'tool' and (self.spec is None or self.executor is None):
            raise ValueError(f'工具 {self.name} 的登记项缺少声明或执行器')


class ToolRegistry:
    """动作与外部工具的统一登记与声明生成。"""

    def __init__(self) -> None:
        """创建一个尚未登记任何条目的空注册表。"""
        self._action_names: Dict[str, None] = {}
        self._action_executors: Dict[str, ToolExecutor] = {}
        self._tools: Dict[str, RegisteredTool] = {}

    def register_action(self, action: ConversationAction) -> None:
        """登记一个封闭动作名。

        :param action: 动作枚举值；其声明与执行都不在本注册表内，登记只为让
            名字进入统一命名空间并在解析时区分动作路径。
        :raises ValueError: 名字与已登记的动作或外部工具重名。
        """
        name = str(action).strip()
        self._reject_duplicate(name)
        self._action_names[name] = None

    def bind_action_executor(self, action: ConversationAction, executor: ToolExecutor) -> None:
        """给一个已登记的认知动作绑定工具执行器。

        终局动作的执行继续走动作头结算路径，不允许绑定执行器；认知动作必须
        绑定，执行阶段解析不到执行器时按装配错误处理，不做任何降级。

        :param action: 动作枚举值，必须已登记且属于认知动作。
        :param executor: 按统一工具协议执行的检索实现。
        :raises ValueError: 动作未登记、不是认知动作或已绑定执行器。
        """
        name = str(action).strip()
        if name not in self._action_names:
            raise ValueError(f'动作 {name} 未登记，无法绑定执行器')
        if action not in COGNITIVE_ACTIONS:
            raise ValueError(f'终局动作 {name} 不允许绑定执行器')
        if name in self._action_executors:
            raise ValueError(f'认知动作 {name} 已绑定执行器，不允许重复绑定')
        self._action_executors[name] = executor

    def register_tool(self, spec: ToolSpec, executor: ToolExecutor) -> None:
        """登记一条外部工具。

        :param spec: 工具声明；参数 Schema 的合法性由调用方保证。
        :param executor: 工具执行器。
        :raises ValueError: 名字与已登记的动作或外部工具重名。
        """
        self._reject_duplicate(spec.name)
        self._tools[spec.name] = RegisteredTool(spec=spec, executor=executor)

    def resolve(self, tool_name: str) -> Optional[ResolvedTool]:
        """按工具名解析登记项。

        :param tool_name: 模型侧给出的工具名。
        :return: 命中的登记项；未登记时返回 None，由消费方决定如何记账
            （解析链路按非法动作处理，不允许静默忽略）。
        """
        name = tool_name.strip()
        if name in self._action_names:
            return ResolvedTool(
                kind='action',
                name=name,
                executor=self._action_executors.get(name),
            )
        registered = self._tools.get(name)
        if registered is not None:
            return ResolvedTool(
                kind='tool',
                name=name,
                spec=registered.spec,
                executor=registered.executor,
            )
        return None

    def action_names(self) -> tuple[str, ...]:
        """返回已登记动作名，按名字排序。

        :return: 动作名元组，供测试与诊断使用。
        """
        return tuple(sorted(self._action_names))

    def build_tool_definitions(self, frame: DecisionFrame) -> List[Dict[str, Any]]:
        """按回合帧生成模型可见的工具声明。

        动作声明与 tool_schema.build_tool_definitions 同源生成——动作空间、
        理由码分域与目标范围的判据只有那一份，本方法不复制。外部工具声明由
        ToolSpec 派生，能力与帧不符的不进入声明；当前没有外部工具接入，
        输出与动作声明逐字一致。

        :param frame: 本回合固定快照。
        :return: OpenAI 兼容的工具声明列表，按名字排序保证请求可复现。
        """
        return list(build_action_tool_definitions(frame))

    def _reject_duplicate(self, name: str) -> None:
        """拒绝与现有登记重名的条目。"""
        if name in self._action_names or name in self._tools:
            raise ValueError(f'工具名 {name} 已被登记，不允许重复注册')


def build_builtin_action_registry() -> ToolRegistry:
    """装配只登记内置动作的注册表。

    六个终局动作与三个认知动作全部来自动作协议的唯一枚举，不在这里复制第二份
    名单；外部工具接入后由组合根继续调用 register_tool。

    :return: 已登记全部内置动作的注册表。
    """
    registry = ToolRegistry()
    for action in TERMINAL_ACTIONS | COGNITIVE_ACTIONS:
        registry.register_action(action)
    return registry
