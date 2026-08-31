"""按回合聚合各级模型调用，并渲染成分层的终端观测面板。

一个回合在多级 Agent 下会产生多次模型调用：决策一次、回复生成一次、认知检索
各一次。控制台原来只看得到最后一条可见产物，出了问题分不清是哪一级——本模块
把每次调用的模型、耗时、落盘记录、完整思考与完整产出收集起来，回合收尾时渲染
成一个嵌套面板。模型响应不在展示层截断，控制台看到的内容与分阶段记录一致。

收集走 ``ContextVar``：回合是一个 asyncio Task，模型路由在同一个 Task 树里执行，
因此 ``ModelRouter`` 只管调用 ``note_model_call``，不需要知道自己属于哪个回合，
并发的多个会话之间也不会互相混淆。

对外暴露 ``begin_turn`` / ``note_model_call`` / ``take_calls`` 与 ``render_stage_panel``，
被 ``src.core.llm_models.router``（写入）与 ``src.core.services.trace_console``
（渲染）使用。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, List

import json

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from src.core.llm_models.openai import error_hint


@dataclass(frozen=True)
class ModelCall:
    """一次模型调用在面板上需要展示的全部事实。

    :ivar task: 模型任务槽名，例如 ``planner`` / ``replyer``；同时是分组依据。
    :ivar model: 实际产出结果的模型名。
    :ivar provider: 实际使用的服务商名。
    :ivar first_token_ms: 首字耗时毫秒；一个分片都没拿到时为 ``None``。
    :ivar total_ms: 本次调用总耗时毫秒。
    :ivar reasoning: 模型的思考文本；未提供时为空串。
    :ivar text: 可见输出文本；工具调用轮为空串。
    :ivar tool_calls: 模型选中的工具调用；非工具轮为空列表。
    :ivar record_path: 分阶段调用记录的落盘路径；未启用记录时为空串。
    :ivar error: 失败描述；成功时为空串。
    :ivar error_kind: 失败类别，用于翻成可照做的说明；成功时为空串。
    """

    task: str
    model: str
    provider: str
    first_token_ms: int | None
    total_ms: int
    reasoning: str = ''
    text: str = ''
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    record_path: str = ''
    error: str = ''
    error_kind: str = ''


@dataclass
class _TurnCallBuffer:
    """保存当前回合仍可接收的模型调用。

    后台任务会继承创建它时的 ``ContextVar``。回合面板取走调用后，把同一个对象
    标记为关闭，迟到的摘要、视觉或记忆任务就能识别自己已不属于该面板，改走独立
    控制台输出；只清空列表会让这些调用追加到一个再也没人读取的旧列表里。
    """

    calls: List[ModelCall] = field(default_factory=list)
    accepting: bool = True


# 当前回合已发生的模型调用。默认 None 表示「不在回合内」——主动消息、日程生成
# 这类调用应独立展示，而不是被误算进某个用户回合。
_calls: ContextVar[_TurnCallBuffer | None] = ContextVar('turn_model_calls', default=None)


def begin_turn() -> None:
    """在当前回合的协程上下文里开启收集。

    副作用：把当前 ContextVar 指向一个新的开放缓冲；同一 Task 内后续的模型调用
        都会记进去，其他 Task 不受影响。
    """
    _calls.set(_TurnCallBuffer())


def note_model_call(call: ModelCall) -> bool:
    """记录一次已完成的模型调用。

    不在回合内或继承到的回合缓冲已经关闭时不收集，由路由层把这次调用独立展示。
    返回收集结果让路由层只选择一个控制台出口，避免同一响应既独立打印又在轮末
    面板重复出现。

    :param call: 已完成调用的展示事实。
    :return: 已收入当前回合面板时为 ``True``；应独立展示时为 ``False``。
    """
    buffer = _calls.get()
    if buffer is None or not buffer.accepting:
        return False
    buffer.calls.append(call)
    return True


def take_calls() -> List[ModelCall]:
    """取出当前回合已收集的调用，并结束收集。

    取走之后把上下文标记回「不在回合内」，而不只是清空列表：回合收尾时派生的
    后台任务（摘要、场景观察）继承的是同一份上下文，只清列表的话它们的模型调用
    会继续追加进来，然后被下一轮的面板当成本轮调用显示出来。

    :return: 按发生顺序排列的调用列表；不在回合内时为空列表。
    副作用：清空收集器并解除回合标记，避免同一批调用被渲染两次。
    """
    buffer = _calls.get()
    _calls.set(None)
    if buffer is None:
        return []
    # 已经派生出去的子任务仍持有这个对象；先关闭，再复制和清空，确保迟到调用
    # 返回 False 并由路由层独立展示，而不是落入无人读取的旧列表。
    buffer.accepting = False
    taken = list(buffer.calls)
    buffer.calls.clear()
    return taken


def render_stage_panel(call: ModelCall) -> Panel:
    """把一次模型调用渲染成一个分级面板。

    :param call: 一次已完成的模型调用。
    :return: 含头部指标、记录路径、思考与产出的面板。
    """
    tint = _TASK_TINTS.get(call.task, 'cyan')
    blocks: List[RenderableType] = [Text('\n'.join(_headline(call)), style=tint)]

    # 路径单独一行且不截断：它是从终端跳到完整请求体的唯一入口，缺失后需自行查找目录。
    # 失败时跳过这一行——下面的失败块会连同「怎么办」一起再给一次路径，这里重复只是噪声。
    if call.record_path and not call.error:
        blocks.append(Text(f'结构化记录：{call.record_path}', style='dim'))

    if call.reasoning.strip():
        blocks.append(Panel(
            Text(call.reasoning.strip(), style='grey70'),
            title=f'完整思考 · {len(call.reasoning)} 字', border_style=tint, padding=(0, 1),
        ))

    for tool_call in call.tool_calls:
        blocks.append(Panel(
            Text(_tool_lines(tool_call), style='yellow'),
            title=f"工具 · {tool_call.get('name', '?')}",
            border_style='yellow', padding=(0, 1),
        ))

    if call.text.strip():
        blocks.append(Panel(
            Text(call.text.strip(), style='bright_green'),
            title=f'完整输出 · {len(call.text)} 字', border_style='green', padding=(0, 1),
        ))

    if call.error:
        # 失败这一级要把「怎么办」和「去哪看完整请求」一次说清：类别本身对人
        # 没有信息量，而存档路径是复现这次调用的唯一入口。
        lines = [f'失败：{call.error}']
        if call.error_kind:
            lines.append(error_hint(call.error_kind))
        if call.record_path:
            lines.append(f'完整请求已存档，把这个文件发出来即可复现：{call.record_path}')
        blocks.append(Text('\n'.join(lines), style='bold red'))

    return Panel(
        Group(*blocks),
        title=_TASK_LABELS.get(call.task, call.task),
        border_style='red' if call.error else tint,
        padding=(0, 1),
    )


def render_timing_footer(calls: List[ModelCall]) -> Text:
    """把各级耗时汇总成一行页脚。

    :param calls: 本回合的全部模型调用。
    :return: 形如 ``决策 2.19 s | 回复生成 9.12 s`` 的单行文本；无调用时为空文本。
    """
    if not calls:
        return Text('')
    parts = [
        f'{_TASK_LABELS.get(call.task, call.task)} {call.total_ms / 1000:.2f} s'
        for call in calls
    ]
    return Text(' | '.join(parts), style='cyan')


# 任务槽到中文名的映射。覆盖当前全部路由任务，确保回合面板和后台独立面板使用
# 同一套名称；未登记的新任务原样显示，不虚构译名。
_TASK_LABELS: Dict[str, str] = {
    'chat': '对话',
    'proactive': '主动对话',
    'summary': '对话摘要',
    'schedule': '日程规划',
    'vision': '视觉理解',
    'expression': '表达模型',
    'planner': '决策',
    'replyer': '回复生成',
    'scene': '群聊场景理解',
    'memory': '记忆抽取',
    'tts': '语音合成',
    'embedding': '向量生成',
}

# 各级的边框色，按「决策冷色、产出暖色」区分层级。
_TASK_TINTS: Dict[str, str] = {
    'chat': 'cyan',
    'proactive': 'bright_cyan',
    'summary': 'blue',
    'schedule': 'yellow',
    'vision': 'blue',
    'expression': 'magenta',
    'planner': 'cyan',
    'replyer': 'green',
    'scene': 'blue',
    'memory': 'bright_magenta',
    'tts': 'bright_green',
    'embedding': 'bright_blue',
}


def _headline(call: ModelCall) -> List[str]:
    """组装面板头部的模型与耗时行。"""
    lines = [f'模型：{call.model}' + (f'（{call.provider}）' if call.provider else '')]
    first_token = (
        f'{call.first_token_ms / 1000:.2f} s' if call.first_token_ms is not None else '—'
    )
    lines.append(f'耗时：首字 {first_token} / 共 {call.total_ms / 1000:.2f} s')
    return lines


def _tool_lines(tool_call: Dict[str, Any]) -> str:
    """把一次工具调用渲染成「名字 + 逐行参数」。

    参数按 JSON 缩进展开：挤成一行的原始 JSON 在终端中难以阅读，而工具参数
    正是这一级唯一的产出。解析失败时原样展示，不推断模型意图。
    """
    raw = str(tool_call.get('arguments', '')).strip()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        formatted = raw
    else:
        formatted = json.dumps(parsed, ensure_ascii=False, indent=2) if parsed else '（无）'
    return f"调用：{tool_call.get('name', '?')}\n{formatted}"
