"""按回合聚合各级模型调用，并渲染成分层的终端观测面板。

一个回合在多级 Agent 下会产生多次模型调用：决策一次、回复生成一次、认知检索
各一次。控制台原来只看得到最后一条可见产物，出了问题分不清是哪一级——本模块
把每次调用的模型、耗时、落盘记录、思考与产出收集起来，回合收尾时渲染成一个
嵌套面板。

收集走 ``ContextVar``：回合是一个 asyncio Task，模型路由在同一个 Task 树里执行，
因此 ``ModelRouter`` 只管调用 ``note_model_call``，不需要知道自己属于哪个回合，
并发的多个会话之间也不会互相串台。

对外暴露 ``begin_turn`` / ``note_model_call`` / ``take_calls`` 与 ``render_stage_panel``，
被 ``src.core.llm_models.router``（写入）与 ``src.core.services.trace_console``
（渲染）使用。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, List

import json

from src.core.llm_models.openai import error_hint

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text


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


# 当前回合已发生的模型调用。默认 None 表示「不在回合内」——主动消息、日程生成
# 这类调用不该被算进某个回合的面板里。
_calls: ContextVar[List[ModelCall] | None] = ContextVar('turn_model_calls', default=None)

# 面板里思考与产出的展示上限。完整内容在 data/logs/prompt/ 的记录里，终端要的是
# 一眼能读完；截断点给得比较宽，是因为思考正是这个面板最值得看的部分。
_REASONING_CHARS = 1200
_TEXT_CHARS = 600


def begin_turn() -> None:
    """在当前回合的协程上下文里开启收集。

    副作用：把当前 ContextVar 指向一个新的空列表；同一 Task 内后续的模型调用
        都会记进去，其他 Task 不受影响。
    """
    _calls.set([])


def note_model_call(call: ModelCall) -> None:
    """记录一次已完成的模型调用。

    不在回合内（未调用过 ``begin_turn``）时静默忽略：主动消息、日程与摘要都走
    同一个路由层，它们不属于任何回合，塞进面板只会让人以为这一轮多调了模型。

    :param call: 已完成调用的展示事实。
    """
    calls = _calls.get()
    if calls is not None:
        calls.append(call)


def take_calls() -> List[ModelCall]:
    """取出当前回合已收集的调用，并结束收集。

    取走之后把上下文标记回「不在回合内」，而不只是清空列表：回合收尾时派生的
    后台任务（摘要、场景观察）继承的是同一份上下文，只清列表的话它们的模型调用
    会继续追加进来，然后被下一轮的面板当成本轮调用显示出来。

    :return: 按发生顺序排列的调用列表；不在回合内时为空列表。
    副作用：清空收集器并解除回合标记，避免同一批调用被渲染两次。
    """
    calls = _calls.get()
    _calls.set(None)
    if not calls:
        return []
    taken = list(calls)
    # 已经派生出去的子任务仍持有这个列表引用，清空它让那些迟到的调用无处可去。
    calls.clear()
    return taken


def render_stage_panel(call: ModelCall) -> Panel:
    """把一次模型调用渲染成一个分级面板。

    :param call: 一次已完成的模型调用。
    :return: 含头部指标、记录路径、思考与产出的面板。
    """
    tint = _TASK_TINTS.get(call.task, 'cyan')
    blocks: List[RenderableType] = [Text('\n'.join(_headline(call)), style=tint)]

    # 路径单独一行且不截断：它是从终端跳到完整请求体的唯一入口，断了就得自己翻目录。
    # 失败时跳过这一行——下面的失败块会连同「怎么办」一起再给一次路径，这里重复只是噪声。
    if call.record_path and not call.error:
        blocks.append(Text(f'结构化记录：{call.record_path}', style='dim'))

    if call.reasoning.strip():
        blocks.append(Panel(
            Text(_clip(call.reasoning, _REASONING_CHARS), style='grey70'),
            title='思考', border_style=tint, padding=(0, 1),
        ))

    for tool_call in call.tool_calls:
        blocks.append(Panel(
            Text(_tool_lines(tool_call), style='yellow'),
            title=f"工具 · {tool_call.get('name', '?')}",
            border_style='yellow', padding=(0, 1),
        ))

    if call.text.strip():
        blocks.append(Panel(
            Text(_clip(call.text, _TEXT_CHARS), style='bright_green'),
            title='输出', border_style='green', padding=(0, 1),
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


# 任务槽到中文名的映射。只覆盖会出现在回合面板里的槽；未列出的原样显示，
# 不硬造译名——新增一级 Agent 时宁可看到英文槽名，也好过看到一个猜出来的词。
_TASK_LABELS: Dict[str, str] = {
    'chat': '对话',
    'planner': '决策',
    'replyer': '回复生成',
    'expression': '表达选择',
    'vision': '视觉',
}

# 各级的边框色，按「决策冷色、产出暖色」区分，扫一眼就知道看的是哪一级。
_TASK_TINTS: Dict[str, str] = {
    'chat': 'cyan',
    'planner': 'cyan',
    'replyer': 'green',
    'expression': 'magenta',
    'vision': 'blue',
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

    参数按 JSON 缩进展开：挤成一行的原始 JSON 在终端里基本读不了，而工具参数
    正是这一级唯一的产出。解析失败时原样展示，不猜模型想写什么。
    """
    raw = str(tool_call.get('arguments', '')).strip()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        formatted = raw
    else:
        formatted = json.dumps(parsed, ensure_ascii=False, indent=2) if parsed else '（无）'
    return f"调用：{tool_call.get('name', '?')}\n{formatted}"


def _clip(text: str, limit: int) -> str:
    """按字符数截断展示文本，保留完整内容在落盘记录里。"""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return f'{stripped[:limit]}…（完整内容见结构化记录）'
