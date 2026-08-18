"""在支持颜色的交互式终端中渲染对话摘要和观察结果。

完整提示词与模型响应由观察事件存储负责记录，本模块只显示有限长度的提示词
预览、响应摘要、侧 effect 和耗时。检测到非交互输出时所有渲染函数保持无操作，
避免把调试面板混入服务日志；终端判断复用 ``common.logger_colors`` 的统一规则。
"""

from __future__ import annotations

from typing import Any
import time

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from src.core.common.log_display import event_label, value_label
from src.core.common.logger import get_logger, is_color_enabled

logger = get_logger(__name__)

_PROMPT_PREVIEW_CHARS = 400

_is_tty = is_color_enabled()
# force_terminal 确保 rich 不因 stdout 是管道而再次禁用颜色；legacy_windows=False
# 让 Windows 管道输出使用 ANSI 序列，由上层终端负责解释。
console = Console(force_terminal=_is_tty or None, legacy_windows=False if _is_tty else None)
_starts: dict[int, float] = {}


def mark_turn_start(turn: int) -> None:
    """记录对话回合开始时间。

    :param turn: 对话回合 ID。

        非交互终端不会写入计时表，因为后续渲染也不会发生。
    """

    if not _is_tty:
        return
    _starts[turn] = time.monotonic()


def _elapsed_ms(turn: int) -> str:
    """取出回合开始时间并格式化耗时。

    :param turn: 对话回合 ID。

    :return: 以毫秒表示的耗时文本；没有开始记录时返回 ``—``。

    副作用：
        消费并删除该回合的开始时间记录。
    """

    started = _starts.pop(turn, None)
    if started is None:
        return '—'
    return f'{(time.monotonic() - started) * 1000:.0f} ms'


def _prompt_preview(messages: list[dict]) -> str:
    """提取系统提示词的有限长度预览。

    :param messages: 对话消息字典列表。

    :return: 最多 400 字符的系统消息预览及消息总数说明。
    """

    system = next((m.get('content') for m in messages if m.get('role') == 'system'), '')
    if not isinstance(system, str):
        system = str(system)
    preview = system[:_PROMPT_PREVIEW_CHARS]
    if len(system) > _PROMPT_PREVIEW_CHARS:
        preview += '…'
    return f'{preview}\n共 {len(messages)} 条消息，完整内容见观察面板'


def _reply_text(segments: list[str]) -> str:
    """把解析器切分出的分句合并为控制台可见正文。

    :param segments: 按 ``<say>`` 边界切分的出站分句列表。

    :return: 非空分句按换行连接后的文本；全部为空时返回空字符串。

    副作用：不修改传入列表。
    """
    return '\n'.join(segment.strip() for segment in segments if segment.strip())


def _side_effect_lines(side_effects: list[dict]) -> list[str]:
    """将记忆、情绪和约定副作用转换为面板行文本。

    :param side_effects: 解析事件产生的副作用字典列表。

    :return: 当前支持的 ``memory_fact``、``mood_delta`` 和 ``promise_stashed``
        副作用行；未知类型被忽略。
    """

    lines = []
    for effect in side_effects:
        if effect.get('kind') == 'memory_fact':
            lines.append(
                f"记忆：[{value_label(str(effect.get('memoryKind', '')))}] "
                f"{effect.get('content', '')}"
            )
        elif effect.get('kind') == 'mood_delta':
            lines.append(
                f"心情：好感 {effect.get('favor')}，精力 {effect.get('energy')}"
            )
        elif effect.get('kind') == 'promise_stashed':
            lines.append(f"约定：{effect.get('subject', '')} → {effect.get('at')}")
    return lines


def render_turn(
    turn: int,
    sender_label: str,
    user_text: str,
    messages: list[dict],
    reply_segments: list[str],
    side_effects: list[dict],
    bot_name: str,
) -> None:
    """渲染一轮对话的摘要面板。

    :param turn: 对话回合 ID。
    :param sender_label: 发送者展示名。
    :param user_text: 用户原始文本。
    :param messages: 发送给模型的消息列表，仅展示系统消息预览。
    :param reply_segments: 解析器按 ``<say>`` 边界切分出的出站分句；模型原始
        输出（含协议标签）由事件账本保留，控制台只展示剥掉标签后的可见正文。
    :param side_effects: 本轮解析出的副作用列表。
    :param bot_name: 主体展示名。

    副作用：
        在交互终端写入 rich 面板；面板渲染异常只记录调试日志，不影响聊天主流程。
    """
    if not _is_tty:
        return
    try:
        # 仅组装有限预览、结构化副作用和解析后的可见正文，完整提示词与原始响应
        # 仍由观察事件存储保留。
        parts: list[Any] = [
            Text.assemble(
                Text('收到消息  ', style='bold cyan'),
                Text(f'{sender_label}：{user_text}', style='bold white'),
            ),
            Text.assemble(
                Text('模型上下文  ', style='bold magenta'),
                Text(_prompt_preview(messages), style='bright_blue'),
            ),
        ]
        reply = _reply_text(reply_segments)
        if reply:
            parts.append(
                Text('机器人回复  ', style='bold green')
                + Text(f'{bot_name}：{reply}', style='bright_green'),
            )
        else:
            parts.append(Text(
                f'机器人回复  {bot_name}：本轮没有可见回复，仅处理内部事件',
                style='italic yellow',
            ))
        # 副作用逐行追加，便于在交互终端中区分记忆写入、情绪变化和约定登记。
        for line in _side_effect_lines(side_effects):
            parts.append(Text('内部变化  ', style='bold yellow') + Text(line, style='bright_yellow'))
        console.print(Panel(
            Group(*parts),
            title=f'第 {turn} 轮对话', subtitle=f'耗时 {_elapsed_ms(turn)}',
            border_style='bright_cyan',
        ))
    except Exception as exc:
        logger.debug('render_turn_failed', error=str(exc))


def render_observation(sender_label: str, user_text: str, reason: str) -> None:
    """以单行显示被回复门控拦截的群消息。

    :param sender_label: 发送者展示名。
    :param user_text: 用户原始文本。
    :param reason: 未回复的机器可读或可读原因。

    副作用：
        在交互终端写入观察行；渲染异常只记录调试日志。
    """
    if not _is_tty:
        return
    try:
        console.print(
            Text('旁听  ', style='bold magenta')
            + Text(f'{sender_label}：', style='bold cyan')
            + Text(user_text, style='white')
            + Text(f'  未回复：{value_label(reason)}', style='yellow'),
        )
    except Exception as exc:
        logger.debug('render_observation_failed', error=str(exc))


def render_action_decision(
    turn: int,
    agent_scope: str,
    event_status: str,
    detail: str = "",
    action: str = "",
    reason_codes: tuple[str, ...] | list[str] = (),
    target_message_ids: tuple[int, ...] | list[int] = (),
) -> None:
    """以单行渲染 Conversation Agent 的行动决策摘要。

    该入口只做观察展示，不触碰事件账本与业务行为；非交互终端自动跳过，
    避免把决策行混入服务日志。

    :param turn: 对话回合 ID。
    :param agent_scope: Agent 灰度路径标识，例如 shadow。
    :param event_status: action_decision 的事件状态。
    :param detail: 失败状态的人类可读原因；成功状态通常为空。
    :param action: 已提交决策的动作名；失败状态为空。
    :param reason_codes: 已提交决策的理由码。
    :param target_message_ids: 已提交决策的目标消息 ID。

    副作用：
        在交互终端写入一行决策摘要；渲染异常只记录调试日志。
    """
    if not _is_tty:
        return
    try:
        scope_label = '影子观察' if agent_scope == 'shadow' else value_label(agent_scope)
        parts: list[Any] = [
            Text('行动决策  ', style='bold magenta'),
            Text(f'{scope_label} · 第 {turn} 轮', style='bold cyan'),
        ]
        if action:
            summary = f'  动作：{value_label(action)}'
            if reason_codes:
                summary += f"  理由：{'、'.join(value_label(code) for code in reason_codes)}"
            if target_message_ids:
                summary += f"  目标消息：{'、'.join(str(target) for target in target_message_ids)}"
            style = 'green' if action == 'reply' else 'yellow'
            parts.append(Text(summary, style=style))
        else:
            summary = f'  状态：{value_label(event_status)}'
            if detail:
                summary += f'  说明：{detail}'
            parts.append(Text(summary, style='bold red'))
        console.print(Text.assemble(*parts))
    except Exception as exc:
        logger.debug('render_action_decision_failed', error=str(exc))


def render_turn_error(
    turn: int,
    sender_label: str,
    user_text: str,
    kind: str,
    message: str,
) -> None:
    """渲染对话回合失败面板。

    :param turn: 对话回合 ID。
    :param sender_label: 发送者展示名。
    :param user_text: 用户原始文本。
    :param kind: 错误类别。
    :param message: 错误消息。

    副作用：
        在交互终端写入错误面板；渲染异常只记录调试日志。
    """

    if not _is_tty:
        return
    try:
        parts = [
            Text(f'收到消息  {sender_label}：{user_text}', style='bold white'),
            Text(f'错误类型  {event_label(kind)}\n错误信息  {message}', style='bold red'),
        ]
        console.print(Panel(
            Group(*parts),
            title=f'第 {turn} 轮对话 · 失败', subtitle=f'耗时 {_elapsed_ms(turn)}',
            border_style='bright_red',
        ))
    except Exception as exc:
        logger.debug('render_turn_error_failed', error=str(exc))
