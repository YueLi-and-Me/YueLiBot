"""统一工具协议的纯数据结构。

本模块定义工具系统的四个不可变数据载体：声明（ToolSpec）、调用（ToolInvocation）、
执行结果（ToolExecutionResult）与执行上下文（ToolContext）。它们只做形状自检，
不读写数据库、不调用模型、不执行任何工具逻辑；登记、过滤与执行的职责分别在
``registry`` 与 ``executor`` 模块。

动作工具（reply / silent / recall 等封闭动作）的声明仍由
``src.core.agent.tool_schema`` 按回合帧同源生成，本模块的 ToolSpec 面向动作集
之外的外部工具；两类工具在注册表里共用同一套登记与解析命名空间。

依赖：``src.core.agent.action_protocol`` 的回合固定快照、``src.core.platform_io.types``
的会话类型、``src.core.common.clock`` 的毫秒时钟；被 ``registry`` / ``executor``
及后续外部工具实现消费，不反向依赖聊天服务或模型层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Literal

from src.core.agent.action_protocol import DecisionFrame
from src.core.common.clock import now as current_time
from src.core.platform_io.types import StreamKind

# 工具语义分类：终局工具结束回合（与动作协议的 TERMINAL_ACTIONS 同域），
# 认知与外部工具执行后把观察回灌模型并消耗一轮认知预算。
ToolKind = Literal['terminal', 'cognitive', 'external']
# 副作用分级：注册表与执行层按此决定工具的启用门槛。readonly 默认可用，
# reversible 需显式启用，irreversible 一律拒绝注册。
ToolSideEffect = Literal['readonly', 'reversible', 'irreversible']

# 单次工具执行的默认超时。执行层用 asyncio.wait_for 约束，避免一个慢工具把
# 整个回合拖死；具体工具可用 timeout_ms 覆盖。
DEFAULT_TOOL_TIMEOUT_MS = 10000


@dataclass(frozen=True)
class ToolSpec:
    """一条外部工具的声明。

    动作工具（封闭动作枚举）不经过本结构：它们的声明按回合帧动态生成，判据只有
    ``tool_schema`` 一份。ToolSpec 描述动作集之外的工具，字段与注册表的过滤
    规则一一对应。

    :ivar name: 工具名，模型可见的协议面；重名登记在注册表直接拒绝。
    :ivar description: 一句话说明，口径是「什么时候用它」，不复述工具名。
    :ivar parameters: OpenAI 兼容的 JSON Schema 对象参数声明。
    :ivar kind: 语义分类，决定执行后回合继续还是结束。
    :ivar side_effect: 副作用分级，决定启用门槛。
    :ivar capabilities: 所需平台能力集合；与回合帧能力不符的工具不进入声明。
    :ivar timeout_ms: 单次执行超时，必须大于 0。
    :ivar metadata: 预留扩展字段，不提前定型字段名。
    :raises ValueError: 工具名为空、超时非正或能力集含空字符串。
    """

    name: str
    description: str = ''
    parameters: Dict[str, Any] = field(default_factory=dict)
    kind: ToolKind = 'external'
    side_effect: ToolSideEffect = 'readonly'
    capabilities: FrozenSet[str] = field(default_factory=frozenset)
    timeout_ms: int = DEFAULT_TOOL_TIMEOUT_MS
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """拒绝空工具名、非正超时与含空字符串的能力集。"""
        if not self.name.strip():
            raise ValueError('工具名不能为空')
        if self.timeout_ms <= 0:
            raise ValueError(f'工具 {self.name} 的超时必须大于 0 毫秒')
        for capability in self.capabilities:
            if not capability.strip():
                raise ValueError(f'工具 {self.name} 的能力集不能含空字符串')


@dataclass(frozen=True)
class ToolInvocation:
    """一次工具调用请求。

    :ivar tool_name: 目标工具名。
    :ivar call_id: 模型侧调用编号；由消费方从模型输出的工具调用还原。
    :ivar arguments: 已解析的参数对象；解析失败不会构造本结构，由调用方按协议
        错误处理。
    :raises ValueError: 工具名为空。
    """

    tool_name: str
    call_id: str = ''
    arguments: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """拒绝缺失工具名的调用。"""
        if not self.tool_name.strip():
            raise ValueError('工具调用必须携带非空工具名')


@dataclass(frozen=True)
class ToolExecutionResult:
    """一次工具执行的结果。

    :ivar tool_name: 被执行工具名，与调用一一对应。
    :ivar success: 执行是否成功。
    :ivar error_message: 失败原因；失败时必填，内容会进入回灌模型的观察文本。
    :ivar observation: 回灌模型的观察正文；终局工具不填。
    :ivar metadata: 预留扩展字段。
    :raises ValueError: 工具名为空，或失败时未给出失败原因。
    """

    tool_name: str
    success: bool
    error_message: str = ''
    observation: str = ''
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """拒绝缺失工具名、或失败却无原因的结果。"""
        if not self.tool_name.strip():
            raise ValueError('工具执行结果必须携带非空工具名')
        if not self.success and not self.error_message.strip():
            raise ValueError(f'工具 {self.tool_name} 执行失败必须给出失败原因')


@dataclass(frozen=True)
class ToolContext:
    """工具执行可读取的会话与回合上下文。

    与认知动作的 CognitiveRequest 对齐并扩充：前者已有的 stream / 人物 / 水位
    信息全部被本结构覆盖，外部工具实现只依赖本结构即可。

    :ivar stream_id: 当前会话分区编号。
    :ivar stream_kind: 会话类型；工具可用此字段自行区分群聊与私聊语义。
    :ivar person_ids: 检索范围覆盖的人物编号；认知工具据此限定事实检索的
        在场者范围，与回合固定快照同一性质：范围在回合开始时定死。
    :ivar frame: 本回合固定快照；含水位、可选消息与平台能力，工具只读不写。
    :ivar turn_id: 回合编号。
    :ivar snapshot_id: 回合快照标识。
    :ivar clock: 毫秒时钟，默认取统一时钟；注入替身便于测试。
    :raises ValueError: 快照标识为空。
    """

    stream_id: int
    stream_kind: StreamKind
    frame: DecisionFrame
    turn_id: int
    snapshot_id: str
    person_ids: tuple[int, ...] = ()
    clock: Callable[[], int] = current_time

    def __post_init__(self) -> None:
        """拒绝缺失快照标识的上下文。"""
        if not self.snapshot_id.strip():
            raise ValueError('工具上下文必须携带非空 snapshot_id')


__all__ = [
    'DEFAULT_TOOL_TIMEOUT_MS',
    'ToolContext',
    'ToolExecutionResult',
    'ToolInvocation',
    'ToolKind',
    'ToolSideEffect',
    'ToolSpec',
]
