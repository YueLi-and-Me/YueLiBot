"""提供跨终端的简洁信息框排版。

启动公告和管线追踪都需要在 Electron 转发、PowerShell 直跑以及 WebUI 日志面板
中保持相同的结构。这里不依赖终端宽度或光标重绘，只生成普通 Unicode 文本框；
完整结构化数据仍由各自的日志/事件账本负责保存。

对外暴露 :func:`display_width`（终端显示宽度）、:func:`render_box`（生成框体文本）、
:func:`print_box`（写控制台并同步 WebUI 日志面板）与 :func:`print_line`（同样两处出口，
但只写一行、不加框）。被 ``logging.logger`` 的管线追踪出口、``db.schema_report``、
``config.upgrade`` 与 ``main`` 的启动公告使用。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import unicodedata

from .logger_colors import (
    RESET_COLOR, is_color_enabled, module_color, normalize_logger_name,
)

from src.core.webui.logs import webui_logs


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


def _paint(text: str, tint: str) -> str:
    """给一段已经排好版的文本套上 ANSI 前景色。

    :param text: 已完成宽度计算的纯文本片段。
    :param tint: ANSI 前景色序列；空串表示不着色。
    :return: 着色后的文本；``tint`` 为空时原样返回，保证无色场景零变化。
    副作用：无。
    """

    return f"{tint}{text}{RESET_COLOR}" if tint else text


def render_box(
    title: str,
    rows: Iterable[Any],
    *,
    width: int = 88,
    tint: str = "",
) -> str:
    """把标题和若干行内容渲染成固定边界的信息框。

    :param title: 信息框标题。
    :param rows: 文本行，或 ``(标签, 值)`` 二元组；值会按 ``str`` 展示。
        必须是不含 ANSI 转义序列的纯文本：排版按显示宽度逐字符计算，
        转义序列会被算进宽度并可能被 :func:`_wrap` 从中间切断。着色由本函数
        统一施加在框线与标题上，调用方不要自己给行内容上色。
    :param width: 目标外框宽度，范围外会被限制到可读区间。
    :param tint: 框线与标题的 ANSI 前景色序列；空串表示不着色，此时输出与
        着色前逐字节一致（重定向到文件与 pytest 下走的就是这一支）。
    :return: 包含顶部、内容和底部边界的多行文本。
    :raises ValueError: ``title`` 为空或 ``rows`` 为空。
    副作用：不写标准输出，不修改传入的行集合。
    """

    normalized_title = str(title).strip()
    if not normalized_title:
        raise ValueError("信息框标题不能为空")
    # 只去右侧空白：左侧缩进是调用方表达层级的手段（数据库结构变更的表名清单就靠
    # 它区分「本次新建的表」与表名本身），两侧都 strip 会把那层结构抹平。
    normalized_rows = [_row_text(row).rstrip() for row in rows]
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
    bottom = f"╰{'─' * (panel_width - 2)}╯"
    # padding 由纯文本 row 算出，着色只包在算完之后的框线上，转义序列因此从不
    # 参与宽度计算，也不会被 _wrap 切断。
    body = [
        f"{_paint('│', tint)} {row}"
        f"{' ' * max(content_width - display_width(row), 0)} {_paint('│', tint)}"
        for row in wrapped_rows
    ]
    return "\n".join([_paint(top, tint), *body, _paint(bottom, tint)])


def print_box(
    title: str,
    rows: Iterable[Any],
    *,
    width: int = 88,
    source: str = "",
    publish: bool = True,
) -> None:
    """把信息框写到标准输出，并（默认）同步发布到 WebUI 日志面板。

    启动公告与数据库结构变更只由本函数呈现：仅 ``print`` 时内容不会进入
    WebUI 实时日志面板，用户在面板中将查不到迁移记录等启动期输出。

    :param title: 信息框标题。
    :param rows: 文本行，或 ``(标签, 值)`` 二元组。
    :param width: 目标外框宽度。
    :param source: 调用方模块名，通常直接传 ``__name__``；用于从模块色表取框线颜色，
        使信息框与该模块的普通日志同色。留空表示不着色。着色与否还受
        :func:`~src.core.logging.logger_colors.is_color_enabled` 的全进程裁定约束。
    :param publish: 是否同时发布到 ``webui_logs``，默认 ``True``。
        框内含密钥时必须显式传 ``False``：WebUI 日志流会把内容推给所有已连接的
        面板并留在内存积压里，而认证 token 的落盘位置受 ``data/runtime/`` 的权限限制，
        日志通道没有那道限制。目前唯一的 ``False`` 调用方是 ``main`` 里两个带登录
        token 的启动公告。
    :return: 无返回值。
    :raises ValueError: ``title`` 为空或 ``rows`` 为空时由 :func:`render_box` 抛出。
    副作用：向标准输出写一个多行信息框并立即刷新；``publish`` 为真时另向
        ``webui_logs`` 发布同一段文本。
    """

    tint = (
        module_color(normalize_logger_name(source))
        if source and is_color_enabled()
        else ""
    )
    text = render_box(title, rows, width=width, tint=tint)
    print(text, flush=True)
    if publish:
        webui_logs.publish(text)


def print_line(text: str, *, tint: str = "", publish: bool = True) -> None:
    """把一行公告写到标准输出，并（默认）同步发布到 WebUI 日志面板。

    与 :func:`print_box` 的分工按内容行数划分：需要成组呈现的多行信息走信息框，
    只有一行的内容走这里——给一行内容加四条框线只会让它更难扫读。

    :param text: 单行公告正文，调用方保证不含换行。
    :param tint: ANSI 前景色序列，整行套用；空串表示不着色。着色与否还受
        :func:`~src.core.logging.logger_colors.is_color_enabled` 的全进程裁定约束，
        由调用方在取色时判断，本函数不重复裁定。
    :param publish: 是否同时发布到 ``webui_logs``，默认 ``True``。含密钥时必须传
        ``False``，理由同 :func:`print_box`。
    :return: 无返回值。
    :raises ValueError: ``text`` 为空或含换行。
    副作用：向标准输出写一行并立即刷新；``publish`` 为真时另向 ``webui_logs``
        发布同一段文本。
    """

    if not text or "\n" in text:
        raise ValueError("print_line 只接受非空单行文本")
    painted = _paint(text, tint)
    print(painted, flush=True)
    if publish:
        webui_logs.publish(painted)


__all__ = ["display_width", "print_box", "print_line", "render_box"]
