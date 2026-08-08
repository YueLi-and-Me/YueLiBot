"""
structlog 日志封装。

所有模块通过 get_logger(__name__) 拿到绑定了模块名的 logger，
不直接调 print() / logging.warning()。

控制台排版：时间戳按级别着色、模块名换成带色的中文别名、级别本身不占一列。
色表与别名在 logger_colors.py。
"""

from __future__ import annotations

from typing import Any, Dict, MutableMapping

import json
import logging

import structlog

from .logger_colors import (
    RESET_COLOR,
    enable_windows_ansi,
    is_color_enabled,
    level_color,
    module_alias,
    module_color,
    normalize_logger_name,
)

from src.webui.logs import webui_logs

# 显式查表，不用 getattr(logging, ...) 兜底：log_level 在 config/schema.py 里是个
# 无校验的 str，写错成 "INF0" 时必须当场报错，而不是悄悄降级成 INFO 让人以为
# 配置生效了。这里是它唯一的把关点。
_LEVELS: Dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# 这几个键在渲染时已经被单独消费掉了，不再重复进 key=value 那一段。
_CONSUMED_KEYS = frozenset({"timestamp", "level", "logger", "logger_name", "event", "exception"})


class ModuleColoredConsoleRenderer:
    """
    按模块着色的控制台渲染器，取代 structlog.dev.ConsoleRenderer。

    一行的构成：

        {时间戳，按级别着色} {[中文别名]，按模块着色} {event 与 k=v，按模块着色}

    级别不单独占一列 —— 它只体现在时间戳的颜色上。级别是一眼扫过去的信息，
    值不得一整列宽度；省下来的八九个字符全给正文。
    """

    def __init__(self, colors: bool = True) -> None:
        self._colors = colors

    def __call__(self, logger: Any, method_name: str, event_dict: MutableMapping[str, Any]) -> str:
        timestamp = str(event_dict.get("timestamp", ""))
        level = str(event_dict.get("level", "info"))
        raw_name = str(event_dict.get("logger", ""))
        name = normalize_logger_name(raw_name)

        color = module_color(name) if self._colors else ""
        parts: list[str] = []

        # 时间戳：按级别着色，warning 变黄、error 变红，扫一眼就能定位问题行
        if timestamp:
            tint = level_color(level) if self._colors else ""
            parts.append(f"{tint}{timestamp}{RESET_COLOR}" if tint else timestamp)

        if name:
            alias = module_alias(name)
            parts.append(f"{color}[{alias}]{RESET_COLOR}" if color else f"[{alias}]")

        body = _stringify(event_dict.get("event", ""))
        parts.append(f"{color}{body}{RESET_COLOR}" if color else body)

        # 结构化字段：logger.info("db_ready", path=...) 里的那些 kwargs
        extras = [
            f"{key}={_stringify(value)}"
            for key, value in event_dict.items()
            if key not in _CONSUMED_KEYS
        ]
        if extras:
            joined = " ".join(extras)
            parts.append(f"{color}{joined}{RESET_COLOR}" if color else joined)

        rendered = " ".join(parts)
        # StackInfoRenderer / format_exc_info 之后异常文本仍在 event_dict 里，
        # 单独换行附在后面，不塞进 key=value 那一段挤成一坨
        exception = event_dict.get("exception")
        if exception:
            return f"{rendered}\n{exception}"
        return rendered


class WebUiLogHandler:
    """structlog 的独立日志支路，始终输出带 ANSI 模块颜色的同款文本。"""

    def __init__(self) -> None:
        self._renderer = ModuleColoredConsoleRenderer(colors=True)

    def __call__(
        self,
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        webui_logs.publish(self._renderer(logger, method_name, event_dict.copy()))
        return event_dict


_webui_log_handler = WebUiLogHandler()


def _stringify(value: Any) -> str:
    """
    值转字符串。

    dict / list 走 json.dumps 且 ensure_ascii=False —— 本项目日志大量带中文
    （人格描述、日程、召回的事实），转义成 \\uXXXX 就没法看了。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def initialize_logging(level: str = "INFO") -> None:
    """
    应用启动时调用一次。

    交互模式（真终端，或被 supervisor 拉起并设了 YUELI_FORCE_COLOR=1）输出上面那套
    彩色人类可读格式；其余情况输出 JSON，方便日志收集与事后 grep。
    判断依据统一走 logger_colors.is_color_enabled()。
    """
    if level.upper() not in _LEVELS:
        raise ValueError(
            f"未知的日志等级 {level!r}，可选：{'、'.join(_LEVELS)}（见 features.toml 的 advanced.log_level）"
        )

    colored = is_color_enabled()

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        # ⚠ 这里**不能**用 structlog.stdlib.add_logger_name。
        #   它读的是 logger.name，而下面用的 PrintLoggerFactory 造出的
        #   PrintLogger 没有 .name 属性 —— 配上去会在**第一次真正打日志时**
        #   抛 AttributeError，而不是在 configure() 时报错。
        #   模块名改由 get_logger() 用 .bind(logger=name) 显式绑定，
        #   与 logger_factory 的选择解耦。
        structlog.stdlib.add_log_level,
        # 时间戳带月日：桌宠是长期挂着的进程，跨天之后只有时分秒
        # 会让人分不清昨天今天。
        structlog.processors.TimeStamper(fmt="%m-%d %H:%M:%S", utc=False),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: Any
    if colored:
        # structlog 自带的 ConsoleRenderer 会在背后 init colorama；换成自己的
        # 渲染器之后得显式做这件事，否则 Windows conhost 下全是转义序列乱码。
        enable_windows_ansi()
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = ModuleColoredConsoleRenderer(colors=True)
    else:
        shared_processors.append(structlog.processors.dict_tracebacks)
        renderer = structlog.processors.JSONRenderer()

    # WebUI 推流是独立处理支路，不能复用 is_color_enabled() 的 TTY 判断。
    # 即使 stdout 是管道或 JSON，这里也始终生成带模块颜色与中文别名的 ANSI 文本。
    shared_processors.append(_webui_log_handler)

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[level.upper()]),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    """
    返回绑定了模块名的 logger。

    模块名走 .bind(logger=...) 而不是 add_logger_name 处理器：
    后者依赖 stdlib logger 的 .name 属性，与 PrintLoggerFactory 不兼容。
    显式绑定则对任何 logger_factory 都成立。
    """
    return structlog.get_logger().bind(logger=name)
