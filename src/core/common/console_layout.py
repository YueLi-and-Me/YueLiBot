"""提供跨终端的简洁信息框排版。

启动公告和管线追踪都需要在 Electron 转发、PowerShell 直跑以及 WebUI 日志面板
中保持相同的结构。这里不依赖终端宽度或光标重绘，只生成普通 Unicode 文本框；
完整结构化数据仍由各自的日志/事件账本负责保存。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import unicodedata


_MIN_PANEL_WIDTH = 48
_MAX_PANEL_WIDTH = 118


def display_width(text: str) -> int:
    """计算文本在常见中文终端中的显示宽度。

    :param text: 不含需要解释的控制序列的文本。
    :return: 中文全角字符按两列、组合字符按零列计算后的宽度。
    副作用：不访问终端，不修改输入文本。
    """

    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _truncate(text: str, width: int) -> str:
    """按显示宽度截断标题或一行内容，并为省略部分保留空间。"""

    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    result: list[str] = []
    used = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        )
        if used + char_width > width - 1:
            break
        result.append(char)
        used += char_width
    return "".join(result) + "…"


def _wrap(text: str, width: int) -> list[str]:
    """按终端显示宽度切分单行文本，不依赖英文单词边界。"""

    if not text:
        return [""]
    lines: list[str] = []
    current: list[str] = []
    used = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        )
        if current and used + char_width > width:
            lines.append("".join(current))
            current = []
            used = 0
        current.append(char)
        used += char_width
    if current:
        lines.append("".join(current))
    return lines


def _row_text(row: Any) -> str:
    """将一项行数据转换成面向人的文本。"""

    if isinstance(row, tuple) and len(row) == 2:
        label, value = row
        return f"{label}：{value}"
    return str(row)


def render_box(
    title: str,
    rows: Iterable[Any],
    *,
    width: int = 88,
) -> str:
    """把标题和若干行内容渲染成固定边界的信息框。

    :param title: 信息框标题。
    :param rows: 文本行，或 ``(标签, 值)`` 二元组；值会按 ``str`` 展示。
    :param width: 目标外框宽度，范围外会被限制到可读区间。
    :return: 包含顶部、内容和底部边界的多行文本。
    :raises ValueError: ``title`` 为空或 ``rows`` 为空。
    副作用：不写标准输出，不修改传入的行集合。
    """

    normalized_title = str(title).strip()
    if not normalized_title:
        raise ValueError("信息框标题不能为空")
    normalized_rows = [_row_text(row).strip() for row in rows]
    if not normalized_rows:
        raise ValueError("信息框至少需要一行内容")

    panel_width = max(_MIN_PANEL_WIDTH, min(int(width), _MAX_PANEL_WIDTH))
    content_width = panel_width - 4
    title_text = _truncate(normalized_title, content_width - 3)
    wrapped_rows: list[str] = []
    for row in normalized_rows:
        wrapped_rows.extend(_wrap(row, content_width))

    title_prefix = f"╭─ {title_text} "
    top = title_prefix + "─" * max(panel_width - display_width(title_prefix) - 1, 0) + "╮"
    body = [
        f"│ {row}{' ' * max(content_width - display_width(row), 0)} │"
        for row in wrapped_rows
    ]
    bottom = f"╰{'─' * (panel_width - 2)}╯"
    return "\n".join([top, *body, bottom])


def print_box(title: str, rows: Iterable[Any], *, width: int = 88) -> None:
    """立即把信息框写到标准输出并刷新，适合启动阶段公告。"""

    print(render_box(title, rows, width=width), flush=True)


__all__ = ["display_width", "print_box", "render_box"]
