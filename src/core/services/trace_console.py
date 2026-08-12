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

    Args:
        turn: 对话回合 ID。

        非交互终端不会写入计时表，因为后续渲染也不会发生。
    """

    if not _is_tty:
        return
    _starts[turn] = time.monotonic()


def _elapsed_ms(turn: int) -> str:
    """取出回合开始时间并格式化耗时。

    Args:
        turn: 对话回合 ID。

    Returns:
        以毫秒表示的耗时文本；没有开始记录时返回 ``—``。

    Side Effects:
        消费并删除该回合的开始时间记录。
    """

    started = _starts.pop(turn, None)
    if started is None:
        return '—'
    return f'{(time.monotonic() - started) * 1000:.0f} ms'


def _prompt_preview(messages: list[dict]) -> str:
    """提取系统提示词的有限长度预览。

    Args:
        messages: 对话消息字典列表。

    Returns:
        最多 400 字符的系统消息预览及消息总数说明。
    """

    system = next((m.get('content') for m in messages if m.get('role') == 'system'), '')
    if not isinstance(system, str):
        system = str(system)
    preview = system[:_PROMPT_PREVIEW_CHARS]
    if len(system) > _PROMPT_PREVIEW_CHARS:
        preview += '…'
    return f'{preview}\n（共 {len(messages)} 条消息，完整内容见观察面板）'


def _side_effect_lines(side_effects: list[dict]) -> list[str]:
    """将记忆和情绪副作用转换为面板行文本。

    Args:
        side_effects: 解析事件产生的副作用字典列表。

    Returns:
        当前支持的 ``memory_fact`` 和 ``mood_delta`` 副作用行；未知类型被忽略。
    """

    lines = []
    for effect in side_effects:
        if effect.get('kind') == 'memory_fact':
            lines.append(f"  · 记忆: [{effect.get('memoryKind', '')}] {effect.get('content', '')}")
        elif effect.get('kind') == 'mood_delta':
            lines.append(f"  · 心情: favor={effect.get('favor')} energy={effect.get('energy')}")
    return lines


def render_turn(
    turn: int,
    sender_label: str,
    user_text: str,
    messages: list[dict],
    response_text: str,
    side_effects: list[dict],
    bot_name: str,
) -> None:
    """渲染一轮对话的摘要面板。

    Args:
        turn: 对话回合 ID。
        sender_label: 发送者展示名。
        user_text: 用户原始文本。
        messages: 发送给模型的消息列表，仅展示系统消息预览。
        response_text: 最终响应文本。
        side_effects: 本轮解析出的副作用列表。
        bot_name: 主体展示名。

    Side Effects:
        在交互终端写入 rich 面板；面板渲染异常只记录调试日志，不影响聊天主流程。
    """
    if not _is_tty:
        return
    try:
        # 仅组装有限预览和结构化副作用，完整提示词仍由观察事件存储保留。
        parts: list[Any] = [
            Text(f'{sender_label}: {user_text}', style='bold'),
            Text(_prompt_preview(messages), style='dim'),
            Text(f'{bot_name}: {response_text}', style='green'),
        ]
        # 副作用逐行追加，便于在交互终端中区分记忆写入和情绪变化。
        for line in _side_effect_lines(side_effects):
            parts.append(Text(line, style='yellow'))
        console.print(Panel(
            Group(*parts),
            title=f'Turn #{turn}', subtitle=_elapsed_ms(turn),
            border_style='cyan',
        ))
    except Exception as exc:
        logger.debug('render_turn_failed', error=str(exc))


def render_observation(sender_label: str, user_text: str, reason: str) -> None:
    """以单行显示被回复门控拦截的群消息。

    Args:
        sender_label: 发送者展示名。
        user_text: 用户原始文本。
        reason: 未回复的机器可读或可读原因。

    Side Effects:
        在交互终端写入观察行；渲染异常只记录调试日志。
    """
    if not _is_tty:
        return
    try:
        console.print(
            Text('· ', style='dim')
            + Text(f'{sender_label}: ', style='dim')
            + Text(user_text, style='dim white')
            + Text(f'  （未回复：{reason}）', style='dim italic'),
        )
    except Exception as exc:
        logger.debug('render_observation_failed', error=str(exc))


def render_turn_error(
    turn: int,
    sender_label: str,
    user_text: str,
    kind: str,
    message: str,
) -> None:
    """渲染对话回合失败面板。

    Args:
        turn: 对话回合 ID。
        sender_label: 发送者展示名。
        user_text: 用户原始文本。
        kind: 错误类别。
        message: 错误消息。

    Side Effects:
        在交互终端写入错误面板；渲染异常只记录调试日志。
    """

    if not _is_tty:
        return
    try:
        parts = [
            Text(f'{sender_label}: {user_text}', style='bold'),
            Text(f'[{kind}] {message}', style='bold red'),
        ]
        console.print(Panel(
            Group(*parts),
            title=f'Turn #{turn} · 失败', subtitle=_elapsed_ms(turn),
            border_style='red',
        ))
    except Exception as exc:
        logger.debug('render_turn_error_failed', error=str(exc))
