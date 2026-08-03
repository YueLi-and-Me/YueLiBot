"""
每轮对话结束时在终端打印一个分区的 rich 面板——不是逐条扁平日志。

参考 MaiBot 的思路（每个推理周期渲染一棵 Panel/Group 树），但按这个项目的
量级简化：对话轮次比它的多阶段流水线简单得多，用一层 Group 摊平展示就够，
不需要嵌套 Panel。完整 prompt/逐 chunk 细节已经写进 trace.jsonl
（services/trace.py），这里只给一眼能扫完的摘要。

★ 不是 TTY（打包后台跑、日志重定向到文件）时全部函数变成空操作——
  这一层是叠加在 structlog 之上的，不能在非交互环境下污染输出。
  TTY 判断同时认 YUELI_FORCE_COLOR=1（见 common/logger.py 的同一条注释）：
  被 Electron 的 supervisor 拉起时 stdout 恒为管道，`isatty()` 恒为 False，
  但这个管道会被逐行转发进真终端，所以需要一个显式信号而不是只看 isatty()。

mark_turn_start 记的 _starts 字典没有主动清理：单用户桌面应用，一个进程
一次顶多几十轮在飞，可以接受；如果某轮因为 interrupt() 半途而废、
_starts 里的条目永远不会被弹出，也只是几十字节常驻内存，不值得为此加复杂度。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from yueli.common.logger import get_logger

logger = get_logger(__name__)

_PROMPT_PREVIEW_CHARS = 400

_is_tty = sys.stdout.isatty() or os.environ.get('YUELI_FORCE_COLOR') == '1'
# force_terminal：rich 自己也会在构造/打印时探测 sys.stdout.isatty()，
# 不强制的话即使上面的 _is_tty 放行，rich 内部还是会因为看到管道而把颜色
# 全部去掉，等于白做上面那道判断。
# legacy_windows=False：Windows 上 rich 拿不到真实控制台句柄时（正是被
# Electron 用管道拉起的情况）会退化成调 Win32 控制台 API 上色，这条路径
# 对着一个管道要么静默不上色、要么在写 emoji 时直接 UnicodeEncodeError
# （已经在 render_turn 里包了 try/except，但根源在这）。强制走 ANSI 转义序列——
# supervisor.ts 会把这些字节原样转发进一个真终端，ANSI 在那边能正常渲染。
console = Console(force_terminal=_is_tty or None, legacy_windows=False if _is_tty else None)
_starts: dict[int, float] = {}


def mark_turn_start(turn: int) -> None:
    if not _is_tty:
        return
    _starts[turn] = time.monotonic()


def _elapsed_ms(turn: int) -> str:
    started = _starts.pop(turn, None)
    if started is None:
        return '—'
    return f'{(time.monotonic() - started) * 1000:.0f} ms'


def _prompt_preview(messages: list[dict]) -> str:
    system = next((m.get('content') for m in messages if m.get('role') == 'system'), '')
    if not isinstance(system, str):
        system = str(system)
    preview = system[:_PROMPT_PREVIEW_CHARS]
    if len(system) > _PROMPT_PREVIEW_CHARS:
        preview += '…'
    return f'{preview}\n（共 {len(messages)} 条消息，完整内容见 trace.jsonl）'


def _side_effect_lines(side_effects: list[dict]) -> list[str]:
    lines = []
    for effect in side_effects:
        if effect.get('kind') == 'memory_fact':
            lines.append(f"  · 记忆: [{effect.get('memoryKind', '')}] {effect.get('content', '')}")
        elif effect.get('kind') == 'mood_delta':
            lines.append(f"  · 心情: favor={effect.get('favor')} energy={effect.get('energy')}")
    return lines


def render_turn(
    turn: int, user_text: str, messages: list[dict], response_text: str, side_effects: list[dict],
) -> None:
    """★ 渲染失败绝不能往外抛——这只是叠加在 chat.py 主流程上的调试展示，
    真出问题（比如非 UTF-8 控制台下 emoji 写入炸掉）也只是终端没打印那个面板，
    不该让 chat.py 的异常处理把一次成功的对话误判成失败。"""
    if not _is_tty:
        return
    try:
        parts: list[Any] = [
            Text(f'你: {user_text}', style='bold'),
            Text(_prompt_preview(messages), style='dim'),
            Text(f'月璃: {response_text}', style='green'),
        ]
        for line in _side_effect_lines(side_effects):
            parts.append(Text(line, style='yellow'))
        console.print(Panel(
            Group(*parts),
            title=f'Turn #{turn}', subtitle=_elapsed_ms(turn),
            border_style='cyan',
        ))
    except Exception as exc:
        logger.debug('render_turn_failed', error=str(exc))


def render_turn_error(turn: int, user_text: str, kind: str, message: str) -> None:
    if not _is_tty:
        return
    try:
        parts = [
            Text(f'你: {user_text}', style='bold'),
            Text(f'[{kind}] {message}', style='bold red'),
        ]
        console.print(Panel(
            Group(*parts),
            title=f'Turn #{turn} · 失败', subtitle=_elapsed_ms(turn),
            border_style='red',
        ))
    except Exception as exc:
        logger.debug('render_turn_error_failed', error=str(exc))
