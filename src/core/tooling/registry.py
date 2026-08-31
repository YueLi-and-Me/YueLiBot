"""统一工具注册表。

职责：
1. 登记动作（封闭动作枚举）与外部工具，重名直接拒绝——工具名是模型可见的
   协议面，冲突必须当场暴露而不是静默覆盖；
2. 认知动作绑定工具执行器：终局动作的执行继续走 ConversationAgent 的动作头
   结算，认知动作统一经 ToolExecutor 执行；
3. 按回合帧生成模型可见的工具声明：动作声明与 ``src.core.agent.tool_schema``
   同源生成，外部工具声明由 ToolSpec 派生并按帧能力过滤；
4. 按工具名解析登记项，供执行阶段区分动作路径与工具路径。

执行都不在本模块：终局动作走 ConversationAgent 的动作头结算；认知动作与外部
工具在这里完成登记、可用性过滤和参数校验后，由 ConversationAgent 经
ToolExecutor 统一执行并把结果回灌下一轮。

依赖：``src.core.agent.action_protocol``（动作枚举与回合帧）、
``src.core.agent.tool_schema``（动作声明同源生成）、``spec`` 与
``executor``；被 ``src.core.services.chat`` 装配并注入
ConversationAgent，不反向依赖聊天服务或模型层。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple

import json

from src.core.agent.action_protocol import (
    COGNITIVE_ACTIONS,
    TERMINAL_ACTIONS,
    ConversationAction,
    DecisionFrame,
    IllegalActionError,
)
from src.core.agent.tool_schema import (
    build_tool_definitions as build_action_tool_definitions,
)

from .executor import ToolExecutor
from .spec import ToolInvocation, ToolSpec


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
        :raises ValueError: 名字与已登记的动作或外部工具重名；或声明的副作用等级
            为 irreversible——不可逆副作用不允许经模型调用触发。
        """
        if spec.side_effect == 'irreversible':
            raise ValueError(f'工具 {spec.name} 具有不可逆副作用，不允许注册')
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

    def has_registered_tools(self) -> bool:
        """判断注册表是否含动作集之外的工具。"""
        return bool(self._tools)

    def available_tool_names(self, frame: DecisionFrame) -> Tuple[str, ...]:
        """返回当前回合真正会声明给模型的外部工具名。"""
        return tuple(sorted(
            name
            for name, registered in self._tools.items()
            if self._tool_available(registered.spec, frame)
        ))

    def build_tool_definitions(self, frame: DecisionFrame) -> List[Dict[str, Any]]:
        """按回合帧生成模型可见的工具声明。

        动作声明与 tool_schema.build_tool_definitions 同源生成——动作空间、
        理由码分域与目标范围的判据只有那一份，本方法不复制。外部工具声明由
        ToolSpec 派生，能力与帧不符、或认知预算已经用尽的不进入声明。外部工具
        追加在动作声明之后，并按名字排序；没有可用外部工具时，输出与原动作声明
        逐字一致。

        :param frame: 本回合固定快照。
        :return: OpenAI 兼容的工具声明列表，按名字排序保证请求可复现。
        """
        definitions = list(build_action_tool_definitions(frame))
        for name in self.available_tool_names(frame):
            spec = self._tools[name].spec
            parameters = spec.parameters or {
                'type': 'object',
                'properties': {},
                'additionalProperties': False,
            }
            definitions.append({
                'type': 'function',
                'function': {
                    'name': spec.name,
                    'description': spec.description,
                    'parameters': parameters,
                },
            })
        return definitions

    def parse_invocation(
        self,
        tool_name: str,
        raw_arguments: str | Mapping[str, Any],
        frame: DecisionFrame,
        call_id: str = '',
    ) -> ToolInvocation:
        """解析并按声明校验一次外部工具调用。"""
        resolved = self.resolve(tool_name)
        if resolved is None:
            raise IllegalActionError(f'模型调用了未登记的工具：{tool_name}')
        if resolved.kind != 'tool' or resolved.spec is None:
            raise IllegalActionError(f'{tool_name} 是动作工具，不能走外部执行路径')
        if not self._tool_available(resolved.spec, frame):
            raise IllegalActionError(f'工具 {tool_name} 在当前回合不可用')
        arguments = _decode_arguments(tool_name, raw_arguments)
        _validate_value(arguments, resolved.spec.parameters, '参数')
        return ToolInvocation(
            tool_name=resolved.name,
            call_id=call_id,
            arguments=arguments,
        )

    @staticmethod
    def _tool_available(spec: ToolSpec, frame: DecisionFrame) -> bool:
        """按剩余认知预算、显式平台能力与副作用等级过滤工具。"""
        if not (frame.available_actions & COGNITIVE_ACTIONS):
            return False
        if not spec.capabilities.issubset(frame.capabilities.tool_capabilities()):
            return False
        return spec.side_effect == 'readonly'

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


def _decode_arguments(
    tool_name: str,
    raw_arguments: Any,
) -> Dict[str, Any]:
    """把模型参数还原为对象，JSON 不完整时按协议错误暴露。"""
    if isinstance(raw_arguments, str):
        try:
            decoded = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise IllegalActionError(
                f'工具 {tool_name} 的 arguments 不是合法 JSON：{exc.msg}'
            ) from exc
    elif isinstance(raw_arguments, Mapping):
        decoded = dict(raw_arguments)
    else:
        raise IllegalActionError(f'工具 {tool_name} 的 arguments 必须是 JSON 对象')
    if not isinstance(decoded, dict):
        raise IllegalActionError(f'工具 {tool_name} 的 arguments 必须是 JSON 对象')
    return decoded


def _validate_value(value: Any, schema: Mapping[str, Any], label: str) -> None:
    """校验内置工具使用的 JSON Schema 子集，错误精确指向字段。"""
    if not schema:
        if value:
            raise IllegalActionError(f'{label}不接受任何字段')
        return
    schema_type = schema.get('type')
    if schema_type == 'object':
        if not isinstance(value, dict):
            raise IllegalActionError(f'{label}必须是对象')
        properties = schema.get('properties', {})
        if not isinstance(properties, Mapping):
            raise ValueError(f'{label} Schema 的 properties 必须是对象')
        required = schema.get('required', [])
        if not isinstance(required, list):
            raise ValueError(f'{label} Schema 的 required 必须是数组')
        missing = [str(name) for name in required if name not in value]
        if missing:
            raise IllegalActionError(f'缺少必填字段：{", ".join(missing)}')
        if schema.get('additionalProperties') is False:
            unknown = sorted(str(name) for name in value if name not in properties)
            if unknown:
                raise IllegalActionError(f'未知字段：{", ".join(unknown)}')
        for name, item in value.items():
            item_schema = properties.get(name)
            if isinstance(item_schema, Mapping):
                _validate_value(item, item_schema, str(name))
        return
    if schema_type == 'array':
        if not isinstance(value, list):
            raise IllegalActionError(f'{label}必须是数组')
        item_schema = schema.get('items', {})
        if not isinstance(item_schema, Mapping):
            raise ValueError(f'{label} Schema 的 items 必须是对象')
        for index, item in enumerate(value):
            _validate_value(item, item_schema, f'{label}[{index}]')
        return
    if schema_type == 'integer':
        if not isinstance(value, int) or isinstance(value, bool):
            raise IllegalActionError(f'{label}必须是整数')
        minimum = schema.get('minimum')
        if isinstance(minimum, (int, float)) and value < minimum:
            raise IllegalActionError(f'{label}不能小于 {minimum}')
    elif schema_type == 'number':
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise IllegalActionError(f'{label}必须是数字')
    elif schema_type == 'string':
        if not isinstance(value, str):
            raise IllegalActionError(f'{label}必须是字符串')
    elif schema_type == 'boolean':
        if not isinstance(value, bool):
            raise IllegalActionError(f'{label}必须是布尔值')
    elif schema_type is not None:
        raise ValueError(f'{label} Schema 使用了不支持的类型：{schema_type}')
    enum = schema.get('enum')
    if isinstance(enum, list) and value not in enum:
        raise IllegalActionError(f'{label}不在允许值范围内')
