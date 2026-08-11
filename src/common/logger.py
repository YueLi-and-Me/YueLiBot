"""
structlog 日志封装。

所有模块通过 get_logger(__name__) 拿到绑定了模块名的 logger，
不直接调 print() / logging.warning()。

控制台排版：时间戳按级别着色、模块名换成带色的中文别名、级别本身不占一列。
色表与别名在 logger_colors.py。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, MutableMapping, TYPE_CHECKING

import json
import logging

import structlog

from .log_sink import JsonlFileSink, render_json_line
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

# 显式查表，不使用 getattr(logging, ...) 兜底；log_level 由配置模型提供字符串值。
# 直接解析字符串，写错成 "INF0" 时必须立即报错，避免错误配置被当成 INFO 使用。
# 日志初始化在此处统一验证并映射级别。
if TYPE_CHECKING:
    from src.config.schema import LogConfig

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

    级别不单独占用列宽，仅通过时间戳颜色表达；释放的列宽用于显示正文内容。
    """

    def __init__(
        self,
        colors: bool = True,
        level_style: str = 'lite',
        color_scope: str = 'full',
    ) -> None:
        """创建按模块和日志级别着色的渲染器。

        :param colors: 是否启用 ANSI 颜色，默认值为 `True`。
        :param level_style: 级别显示方式，支持 `lite`、`compact` 或完整大写，默认值为 `lite`。
        :param color_scope: 颜色覆盖范围，默认值为 `full`；`lite` 仅着色标题区域。
        :side_effects: 保存渲染选项，不配置全局 structlog。
        """
        self._colors = colors
        self._level_style = level_style
        self._color_scope = color_scope

    def __call__(self, logger: Any, method_name: str, event_dict: MutableMapping[str, Any]) -> str:
        """把 structlog 事件字典渲染为单行控制台文本。

        :param logger: structlog logger 实例；当前实现不读取其属性。
        :param method_name: 调用的日志方法名，例如 `info` 或 `error`。
        :param event_dict: 已由前置处理器补充字段的可变事件字典。
        :return: 带时间、模块别名、事件正文和结构化字段的文本；异常字段单独换行。
        :side_effects: 只读取事件字典，不修改它。
        :performance: 对事件字段执行一次线性遍历和必要的 JSON 序列化。
        """
        timestamp = str(event_dict.get("timestamp", ""))
        level = str(event_dict.get("level", "info"))
        raw_name = str(event_dict.get("logger", ""))
        name = normalize_logger_name(raw_name)

        color = module_color(name) if self._colors else ""
        parts: list[str] = []

        # 时间戳按日志级别着色，warning 使用黄色、error 使用红色，便于按级别筛查日志。
        if timestamp:
            tint = level_color(level) if self._colors else ""
            parts.append(f"{tint}{timestamp}{RESET_COLOR}" if tint else timestamp)

        if self._level_style != 'lite':
            tag = level[:1].upper() if self._level_style == 'compact' else level.upper()
            tint = level_color(level) if self._colors else ""
            parts.append(f"{tint}{tag}{RESET_COLOR}" if tint else tag)

        if name:
            alias = module_alias(name)
            parts.append(f"{color}[{alias}]{RESET_COLOR}" if color else f"[{alias}]")

        # title 只染时间戳和模块名，正文保持终端默认色
        body_color = color if self._color_scope == 'full' else ""
        body = _stringify(event_dict.get("event", ""))
        parts.append(f"{body_color}{body}{RESET_COLOR}" if body_color else body)

        # 结构化字段：logger.info("db_ready", path=...) 里的那些 kwargs
        extras = [
            f"{key}={_stringify(value)}"
            for key, value in event_dict.items()
            if key not in _CONSUMED_KEYS
        ]
        if extras:
            joined = " ".join(extras)
            parts.append(f"{body_color}{joined}{RESET_COLOR}" if body_color else joined)

        rendered = " ".join(parts)
        # StackInfoRenderer / format_exc_info 之后异常文本仍在 event_dict 里，
        # 单独换行附在后面，不塞进 key=value 那一段挤成一坨
        exception = event_dict.get("exception")
        if exception:
            return f"{rendered}\n{exception}"
        return rendered


class WebUiLogHandler:
    """把结构化日志复制到 WebUI 日志订阅器，并保留 ANSI 模块颜色。"""

    def __init__(self) -> None:
        """创建使用彩色控制台渲染器的 WebUI 处理器。

        :side_effects: 创建一个独立渲染器，不注册全局处理器。
        """
        self._renderer = ModuleColoredConsoleRenderer(colors=True)

    def __call__(
        self,
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """发布当前事件的渲染文本并原样返回事件字典。

        :param logger: structlog logger 实例。
        :param method_name: 日志方法名。
        :param event_dict: 当前事件字典。
        :return: 原始 `event_dict`，供后续处理器继续使用。
        :side_effects: 向 `webui_logs` 发布一条日志。
        """
        webui_logs.publish(self._renderer(logger, method_name, event_dict.copy()))
        return event_dict


class FileLogHandler:
    """按文件等级过滤并把结构化日志写入 JSONL sink。"""

    def __init__(self, sink: JsonlFileSink, level: int) -> None:
        """创建文件日志处理器。

        :param sink: 接收落盘事件的 JSONL sink。
        :param level: 允许写入的最低 Python logging 等级。
        :side_effects: 保存 sink 和等级，不写入日志。
        """
        self._sink = sink
        self._level = level

    def __call__(
        self,
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """按等级写入当前事件并把事件字典继续传递。

        :param logger: structlog logger 实例。
        :param method_name: 日志方法名。
        :param event_dict: 当前事件字典。
        :return: 原始 `event_dict`。
        :side_effects: 事件等级达到阈值时向 sink 写入一行。
        """
        if _LEVELS.get(str(event_dict.get('level', 'info')).upper(), logging.INFO) >= self._level:
            self._sink.write(render_json_line(event_dict))
        return event_dict


_webui_log_handler = WebUiLogHandler()
_file_sink: JsonlFileSink | None = None


def current_log_file() -> Path | None:
    """返回当前 JSONL sink 正在写入的日志文件路径。

    :return: 当前文件路径；未初始化文件 sink 或尚未写入时返回 `None`。
    :side_effects: 不执行文件 I/O。
    """
    return _file_sink.current_path() if _file_sink is not None else None


def _stringify(value: Any) -> str:
    """将日志字段转换为适合控制台展示的字符串。

    字典和列表使用 ``ensure_ascii=False`` 的 JSON 编码，以保留人格、日程和记忆
    文本中的中文字符。

    Args:
        value: 任意日志字段值。

    Returns:
        字符串原样返回；字典和列表返回非 ASCII 转义 JSON；其他值返回 ``str(value)``。

    Raises:
        TypeError: 字典或列表包含无法 JSON 序列化的值时抛出。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class _ConsoleLevelGate:
    """丢弃低于控制台等级的事件，同时保留文件和 WebUI 支路已接收的副本。"""

    def __init__(self, level: int) -> None:
        """创建控制台等级过滤器。

        :param level: 允许继续渲染的最低 Python logging 等级。
        :side_effects: 保存等级，不修改全局日志配置。
        """
        self._level = level

    def __call__(
        self,
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        """按事件等级决定继续渲染或抛出 structlog 丢弃信号。

        :param logger: structlog logger 实例。
        :param method_name: 日志方法名。
        :param event_dict: 当前事件字典。
        :return: 达到等级阈值时返回原字典。
        :raises structlog.DropEvent: 事件等级低于控制台阈值。
        :side_effects: 不修改事件字典。
        """
        if _LEVELS.get(str(event_dict.get('level', 'info')).upper(), logging.INFO) < self._level:
            raise structlog.DropEvent
        return event_dict


def _resolve_level(name: str, field: str) -> int:
    """将配置中的日志级别名称解析为 Python logging 数值。

    Args:
        name: 日志级别名称，不区分大小写。
        field: 配置字段路径，用于构造错误信息。

    Returns:
        对应的 Python logging 等级整数。

    Raises:
        ValueError: 名称不在支持的日志等级集合中；函数不会静默降级为 INFO。
    """
    try:
        return _LEVELS[name.upper()]
    except KeyError:
        raise ValueError(
            f"未知的日志等级 {name!r}，可选：{'、'.join(_LEVELS)}（见 features.toml 的 {field}）"
        ) from None


def _apply_library_levels(config: LogConfig, fallback: int) -> None:
    """应用第三方库日志等级和抑制列表。

    Args:
        config: 包含库级别和抑制库名称的日志配置。
        fallback: 根 logger 在未单独配置时使用的日志等级。

    Raises:
        ValueError: 某个库级别名称无法解析。

    Side Effects:
        修改指定 logger 的 handler、传播标志、禁用状态和日志等级，并更新根 logger 等级。
    """
    for name in config.suppress_libraries:
        library = logging.getLogger(name)
        library.handlers.clear()
        library.propagate = False
        library.disabled = True
    for name, level_name in config.library_levels.items():
        logging.getLogger(name).setLevel(
            _resolve_level(level_name, f'log.library_levels.{name}')
        )
    logging.getLogger().setLevel(fallback)


def initialize_logging(config: LogConfig | None = None, log_dir: Path | None = None) -> None:
    """按配置初始化控制台、文件和 WebUI 三条日志输出支路。

    控制台根据终端能力输出彩色文本或 JSON，文件始终使用 JSONL；未提供日志目录
    或关闭文件输出时不创建文件 sink。

    Args:
        config: 日志配置；省略时创建默认 ``LogConfig``。
        log_dir: 可选日志目录；为 ``None`` 时禁用文件落盘。

    Raises:
        ValueError: 任一日志级别名称无法解析。
        OSError: 文件日志目录或滚动文件无法创建。

    Side Effects:
        修改 structlog 全局处理器和第三方库 logger 配置，可能创建文件日志 sink；
        重复调用会替换当前文件 sink。
    """
    global _file_sink

    if config is None:
        # 延迟到函数内导入：config 依赖 common.logger，模块层反向依赖会成环
        from src.config.schema import LogConfig
        config = LogConfig()
    level = _resolve_level(config.level, 'log.level')
    console_level = _resolve_level(config.console_level, 'log.console_level')         if config.console_level else level
    file_level = _resolve_level(config.file_level, 'log.file_level')         if config.file_level else level

    colored = is_color_enabled() and config.color_scope != 'none'

    shared_processors: List[Any] = [
        structlog.contextvars.merge_contextvars,
        # 不使用 add_logger_name：PrintLoggerFactory 生成的 logger 没有 name 属性。
        # 模块名由 get_logger() 显式绑定，避免处理器依赖具体 logger factory。
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt=config.date_format, utc=False),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: Any
    if colored:
        # structlog 自带的 ConsoleRenderer 会在背后 init colorama；换成自己的
        # 渲染器之后得显式做这件事，否则 Windows conhost 下全是转义序列乱码。
        enable_windows_ansi()
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = ModuleColoredConsoleRenderer(
            colors=True,
            level_style=config.level_style,
            color_scope=config.color_scope,
        )
    else:
        shared_processors.append(structlog.processors.dict_tracebacks)
        renderer = structlog.processors.JSONRenderer()

    # 落盘要在渲染成字符串之前挂上，拿到的才是结构化字典
    _file_sink = None
    if config.to_file and log_dir is not None:
        _file_sink = JsonlFileSink(
            log_dir,
            max_bytes=config.file_max_bytes,
            max_files=config.max_files,
            cleanup_days=config.cleanup_days,
        )
        shared_processors.append(FileLogHandler(_file_sink, file_level))

    # WebUI 推流是独立支路，不复用 is_color_enabled() 的 TTY 判断：
    # 即使 stdout 是管道，这里也始终生成带模块颜色的 ANSI 文本。
    shared_processors.append(_webui_log_handler)
    shared_processors.append(_ConsoleLevelGate(console_level))

    # 三条支路取最低门槛，各自再按自己的等级筛
    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            min(level, console_level, file_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    _apply_library_levels(config, level)



def get_logger(name: str):
    """返回绑定当前模块名的 structlog logger。

    使用 ``bind(logger=...)`` 而不是依赖 stdlib logger 的 ``name`` 属性，确保与
    当前 PrintLoggerFactory 兼容。

    Args:
        name: 要写入结构化日志的模块名。

    Returns:
        绑定了 ``logger=name`` 字段的 structlog logger。
    """
    return structlog.get_logger().bind(logger=name)
