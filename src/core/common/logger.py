"""
structlog 日志封装。

所有模块通过 get_logger(__name__) 拿到绑定了模块名的 logger，
不直接调 print() / logging.warning()。

控制台排版：时间戳、中文模块名、中文事件和结构化字段分层着色，字段之间使用
固定分隔符留出呼吸感。色表与别名在 logger_colors.py，协议中文映射在
log_display.py；JSONL 文件继续保留英文机器标识。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, MutableMapping, TYPE_CHECKING

import logging

import structlog

from .console_layout import display_width, render_box
from .log_sink import JsonlFileSink, render_json_line
from .log_display import display_value, event_label, field_label, value_label
from .logger_colors import (
    EVENT_COLOR,
    FIELD_LABEL_COLOR,
    FIELD_VALUE_COLOR,
    RESET_COLOR,
    SEPARATOR_COLOR,
    enable_windows_ansi,
    is_color_enabled,
    level_color,
    module_alias,
    module_color,
    normalize_logger_name,
)

from src.core.webui.logs import webui_logs

# 显式查表，不使用 getattr(logging, ...) 兜底；log_level 由配置模型提供字符串值。
# 直接解析字符串，写错成 "INF0" 时必须立即报错，避免错误配置被当成 INFO 使用。
# 日志初始化在此处统一验证并映射级别。
if TYPE_CHECKING:
    from src.core.config.schema import LogConfig

_LEVELS: Dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# 这几个键在渲染时已经被单独消费掉了，不再重复进 key=value 那一段。
_CONSUMED_KEYS = frozenset({"timestamp", "level", "logger", "logger_name", "event", "exception"})

_LEVEL_LABELS: Dict[str, str] = {
    "debug": "调试",
    "info": "信息",
    "warning": "警告",
    "error": "错误",
    "critical": "严重",
}


class ModuleColoredConsoleRenderer:
    """
    按模块着色的控制台渲染器，取代 structlog.dev.ConsoleRenderer。

    一行的构成：

        {时间戳} {[中文模块]} {中文事件} │ {中文字段：值} │ {中文字段：值}

    模块色只标识来源，正文使用固定高对比色；避免低亮模块把整段正文染成灰色。
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
        副作用：保存渲染选项，不配置全局 structlog。
        """
        self._colors = colors
        self._level_style = level_style
        self._color_scope = color_scope

    def __call__(self, logger: Any, method_name: str, event_dict: MutableMapping[str, Any]) -> str:
        """把 structlog 事件字典渲染为单行控制台文本。

        :param logger: structlog logger 实例；当前实现不读取其属性。
        :param method_name: 调用的日志方法名，例如 `info` 或 `error`。
        :param event_dict: 已由前置处理器补充字段的可变事件字典。
        :return: 带时间、模块别名、中文事件和分段字段的文本；异常字段单独换行。
        副作用：只读取事件字典，不修改它。
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
            label = _LEVEL_LABELS.get(level.lower(), level.upper())
            tag = label[:1] if self._level_style == 'compact' else label
            tint = level_color(level) if self._colors else ""
            decorated = f"[{tag}]"
            parts.append(f"{tint}{decorated}{RESET_COLOR}" if tint else decorated)

        if name:
            alias = module_alias(name)
            parts.append(f"{color}[{alias}]{RESET_COLOR}" if color else f"[{alias}]")

        # 机器事件名只在展示层翻译；文件 sink 在本处理器之前已接收原始事件字典。
        body = event_label(_stringify(event_dict.get("event", "")))
        body_color = EVENT_COLOR if self._colors and self._color_scope == 'full' else ""
        parts.append(f"{body_color}{body}{RESET_COLOR}" if body_color else body)

        # 字段名与值分别着色，并用竖线隔开。比连续的 key=value 更容易扫读，也能在
        # WebUI 自动换行时保留清晰边界。
        extras: list[str] = []
        for key, value in event_dict.items():
            if key in _CONSUMED_KEYS:
                continue
            label = field_label(key)
            value_text = (
                event_label(_stringify(value))
                if key == "kind"
                else _stringify(value)
            )
            if self._colors and self._color_scope == 'full':
                extras.append(
                    f"{FIELD_LABEL_COLOR}{label}{RESET_COLOR}："
                    f"{FIELD_VALUE_COLOR}{value_text}{RESET_COLOR}"
                )
            else:
                extras.append(f"{label}：{value_text}")
        if extras:
            separator = " │ "
            if self._colors and self._color_scope == 'full':
                separator = f" {SEPARATOR_COLOR}│{RESET_COLOR} "
            parts.append(separator.join(extras))

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

        副作用：创建一个独立渲染器，不注册全局处理器。
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
        副作用：向 `webui_logs` 发布一条日志。
        """
        webui_logs.publish(self._renderer(logger, method_name, event_dict.copy()))
        return event_dict


class FileLogHandler:
    """按文件等级过滤并把结构化日志写入 JSONL sink。"""

    def __init__(self, sink: JsonlFileSink, level: int) -> None:
        """创建文件日志处理器。

        :param sink: 接收落盘事件的 JSONL sink。
        :param level: 允许写入的最低 Python logging 等级。
        副作用：保存 sink 和等级，不写入日志。
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
        副作用：事件等级达到阈值时向 sink 写入一行。
        """
        if _LEVELS.get(str(event_dict.get('level', 'info')).upper(), logging.INFO) >= self._level:
            self._sink.write(render_json_line(event_dict))
        return event_dict


_webui_log_handler = WebUiLogHandler()
_file_sink: JsonlFileSink | None = None


def current_log_file() -> Path | None:
    """返回当前 JSONL sink 正在写入的日志文件路径。

    :return: 当前文件路径；未初始化文件 sink 或尚未写入时返回 `None`。
    副作用：不执行文件 I/O。
    """
    return _file_sink.current_path() if _file_sink is not None else None


def _flatten(text: str) -> str:
    """压平日志文本里的换行与制表符，保证一条日志在终端只占一行。

    提示词、模型响应等字段自带大量换行；不压平时一条日志会把控制台
    输出撑成几十行，与后续日志交错后无法阅读。字面反斜杠转义保持原样，
    避免误改 Windows 路径等普通文本。

    :param text: 待压平的日志字段文本。

    :return: 换行、回车、制表符全部替换为空格后的单行文本。

    副作用：不修改原字符串。
    """
    return (
        text
        .replace('\n', ' ')
        .replace('\r', ' ')
        .replace('\t', ' ')
    )


def _stringify(value: Any) -> str:
    """将日志字段转换为适合控制台展示的字符串。

    字典、列表、布尔值和常见协议枚举经展示层转换为紧凑中文文本；普通字符串
    原样保留。

    :param value: 任意日志字段值。

    :return: 展示文本统一经过 :func:`_flatten` 压平为单行。

    :raises TypeError: 字典或列表包含无法 JSON 序列化的值时抛出。
    """
    return _flatten(display_value(value))


# 管线 trace 事件的控制台出口。``src.core.observe.events`` 在 main 启动早期被导入，
# 这里不依赖其模块级 logger，直接生成面板文本；事件账本仍保存完整字段。
_trace_date_format = '%m-%d %H:%M:%S'
# LIVE_ONLY 事件高频且本身就是给观察面板的实时流，控制台逐条打印会淹没其它日志。
_trace_console_silent_kinds = frozenset({'llm_chunk', 'foreground'})

# 来源元数据在同一轮的每条事件里都会重复出现；完整来源仍写入事件账本，控制台只
# 在需要时把发送者放进面板标题。这样日志不会被账号、群名片和昵称字段横向撑开。
_TRACE_ORIGIN_FIELDS = frozenset({
    'platform',
    'personId',
    'personKind',
    'senderExternalId',
    'senderNickname',
    'senderGroupCard',
    'senderDisplayName',
    'senderLabel',
    'botName',
})

# 高频事件按诊断重点保留字段；未知事件则保留全部非来源字段，避免新增事件完全失去
# 控制台可见性。
_TRACE_VISIBLE_FIELDS: Dict[str, tuple[str, ...]] = {
    'stage': ('stageLabel', 'streamName', 'detail', 'turnId'),
    'user_input': ('turnId', 'text', 'externalMessageId'),
    'reply_gate': (
        'turnId', 'streamKind', 'text', 'accepted', 'disposition', 'reason',
        'reasonCodes', 'gateReasonCodes', 'asleep', 'mentionedMe', 'nameMentioned',
        'repliesInWindow', 'maxRepliesInWindow', 'naturalReplyElapsedMs',
    ),
    'llm_request': (
        'turnId', 'messages', 'temperature', 'maxTokens', 'promptId', 'promptHash',
        'renderParams', 'modelTask', 'task',
    ),
    'llm_final': ('turnId', 'text'),
    'prompt_record': ('task', 'path'),
    'llm_error': ('turnId', 'errorKind', 'message'),
    'image_description': ('result', 'hash', 'text', 'promptId', 'error'),
    'action_decision': (
        'turnId', 'eventStatus', 'detail', 'gate', 'decision', 'version',
        'snapshotId', 'messageWatermark',
    ),
    'sleep_transition': ('asleep', 'drowsy', 'probability'),
    'interest': ('interest', 'factors'),
    'memory_fact': ('turnId', 'memoryKind', 'content'),
    'mood_delta': ('turnId', 'favor', 'energy'),
    'promise_stashed': ('turnId', 'subject', 'at'),
}

_TRACE_HASH_FIELDS = frozenset({'hash', 'promptHash', 'contentHash', 'fingerprint'})
_TRACE_PANEL_WIDTH = 112
_TRACE_ROW_WIDTH = _TRACE_PANEL_WIDTH - 8


def _compact_nested_trace_value(key: str, value: Any) -> Any:
    """压缩行动审计中的嵌套结构，保留控制台需要的决策结论。"""

    if not isinstance(value, dict):
        return value
    if key == 'gate':
        disposition = value_label(str(value.get('disposition', '')))
        reasons = display_value(value.get('reasonCodes', []))
        return f'{disposition} · 理由：{reasons}'
    if key == 'decision':
        action = value_label(str(value.get('action', '')))
        length = (
            value.get('reply', {}).get('length')
            if isinstance(value.get('reply'), dict)
            else None
        )
        target_ids = display_value(value.get('targetMessageIds', []))
        parts = [action]
        if length:
            parts.append(f'篇幅：{value_label(str(length))}')
        if target_ids != '无':
            parts.append(f'目标：{target_ids}')
        return ' · '.join(parts)
    if key == 'version':
        task = value.get('modelTask') or '—'
        model = value.get('model') or '—'
        latency = value.get('latencyMs')
        suffix = f' · 耗时：{latency} ms' if latency else ''
        return f'{task} · {model}{suffix}'
    return value


def _trace_value_text(kind: str, key: str, value: Any) -> Any:
    """生成单条追踪字段的紧凑展示值，不改变账本中的原始字段。"""

    if key == 'messages':
        messages = value
        total_chars = sum(len(str(message.get('content', ''))) for message in messages)
        return f'{len(messages)} 条消息、共 {total_chars} 字（完整提示词见观察面板）'
    if key == 'renderParams':
        return '、'.join(value)
    if key == 'text':
        suffix = '完整消息' if kind in {'user_input', 'reply_gate', 'observation'} else '完整响应'
        return f'{len(str(value or ""))} 字（{suffix}见观察面板）'
    if key in _TRACE_HASH_FIELDS:
        text = str(value)
        return f'{text[:12]}…' if len(text) > 16 else text
    return _compact_nested_trace_value(key, value)


def _summarize_trace_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """把 trace 字段压缩成控制台摘要，避免重复元数据占满多行。

    :param fields: 已去掉时间和序号字段、仍保留 ``kind`` 的事件字段。

    :return: 按事件类型保留关键字段，并把提示词、正文、哈希和行动嵌套结构替换为
        紧凑摘要；完整字段仍保留在事件账本中。

    副作用：不修改传入字典。
    """
    kind = str(fields.get('kind', ''))
    visible = _TRACE_VISIBLE_FIELDS.get(kind)
    keys = list(visible or ())
    keys.extend(key for key in fields if key not in keys)
    summary: Dict[str, Any] = {}
    for key in keys:
        if key not in fields:
            continue
        value = fields[key]
        # 空字段和重复的机器阶段 ID 不进入控制台；完整事件仍保留在账本中。
        if value is None or value == "" or value == [] or value == {}:
            continue
        if key == 'kind':
            continue
        if key == "stage" and fields.get("stageLabel"):
            continue
        if key in _TRACE_ORIGIN_FIELDS and (visible is None or key not in visible):
            continue
        if visible is not None and key not in visible and key not in {'stageLabel', 'streamName'}:
            continue
        summary[key] = _trace_value_text(kind, key, value)
    return summary


def _pack_trace_rows(summary: Dict[str, Any]) -> list[str]:
    """把追踪字段按可读宽度合并成两列或多列面板行。"""

    rows: list[str] = []
    current = ''
    for key, value in summary.items():
        item = f'{field_label(key)}：{_stringify(value)}'
        if current and display_width(current) + 4 + display_width(item) <= _TRACE_ROW_WIDTH:
            current += f'  │  {item}'
            continue
        if current:
            rows.append(current)
        current = item
    if current:
        rows.append(current)
    return rows or ['—']


def emit_console_trace(entry: MutableMapping[str, Any]) -> None:
    """把一条管线 trace 事件渲染成紧凑信息框并打到控制台与 WebUI 日志面板。

    该出口绕开 ``src.core.observe.events`` 模块级 logger，保证 trace 不会因为导入
    时机落回旧式 structlog 行。事件账本与观察面板订阅者由调用方另行处理，本函数
    只负责控制台与日志面板这两条展示支路。

    :param entry: ``emit`` 已组装完的事件字典，包含 ``at``、``kind`` 等保留字段。

    副作用：
        向标准输出写一个多行信息框，并把同一段文本发布到
        ``webui_logs``；``llm_chunk``、``foreground`` 等高频实时事件被静音，
        只广播不进控制台。
    """
    if entry.get('kind') in _trace_console_silent_kinds:
        return
    # 带 turnId 的管线事件已由 trace_console 的轮末合成面板整体呈现，这里不再逐条打纯文本框，
    # 避免同一轮既出嵌套大面板又出一串小框。事件账本仍保留完整事件，观察面板订阅不受影响；
    # 无 turnId 的管线事件（如部分主动感知事件）仍按原样出框，保留其控制台可见性。
    if entry.get('turnId') is not None:
        return
    from datetime import datetime
    timestamp = datetime.fromtimestamp((entry.get('at') or 0) / 1000).strftime(_trace_date_format)
    fields: Dict[str, Any] = {
        key: value for key, value in entry.items()
        if key not in {'at', 'seq'}
    }
    summary = _summarize_trace_fields(fields)
    kind = str(fields.get('kind', ''))
    event_name = (
        str(fields.get('stageLabel') or '')
        if kind == 'stage'
        else event_label(kind)
    )
    title = f'{timestamp} · 运行追踪 · {event_name}'
    sender = str(fields.get('senderDisplayName') or '').strip()
    if sender:
        title += f' · {sender}'
    line = render_box(title, _pack_trace_rows(summary), width=_TRACE_PANEL_WIDTH)
    print(line)
    webui_logs.publish(line)


class _ConsoleLevelGate:
    """丢弃低于控制台等级的事件，同时保留文件和 WebUI 支路已接收的副本。"""

    def __init__(self, level: int) -> None:
        """创建控制台等级过滤器。

        :param level: 允许继续渲染的最低 Python logging 等级。
        副作用：保存等级，不修改全局日志配置。
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
        副作用：不修改事件字典。
        """
        if _LEVELS.get(str(event_dict.get('level', 'info')).upper(), logging.INFO) < self._level:
            raise structlog.DropEvent
        return event_dict


def _resolve_level(name: str, field: str) -> int:
    """将配置中的日志级别名称解析为 Python logging 数值。

    :param name: 日志级别名称，不区分大小写。
    :param field: 配置字段路径，用于构造错误信息。

    :return: 对应的 Python logging 等级整数。

    :raises ValueError: 名称不在支持的日志等级集合中；函数不会静默降级为 INFO。
    """
    try:
        return _LEVELS[name.upper()]
    except KeyError:
        raise ValueError(
            f"未知的日志等级 {name!r}，可选：{'、'.join(_LEVELS)}（见 features.toml 的 {field}）"
        ) from None


def _apply_library_levels(config: LogConfig, fallback: int) -> None:
    """应用第三方库日志等级和抑制列表。

    :param config: 包含库级别和抑制库名称的日志配置。
    :param fallback: 根 logger 在未单独配置时使用的日志等级。

    :raises ValueError: 某个库级别名称无法解析。

    副作用：
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

    :param config: 日志配置；省略时创建默认 ``LogConfig``。
    :param log_dir: 可选日志目录；为 ``None`` 时禁用文件落盘。

    :raises ValueError: 任一日志级别名称无法解析。
    :raises OSError: 文件日志目录或滚动文件无法创建。

    副作用：
        修改 structlog 全局处理器和第三方库 logger 配置，可能创建文件日志 sink；
        重复调用会替换当前文件 sink。
    """
    global _file_sink
    global _trace_date_format

    if config is None:
        # 延迟到函数内导入：config 依赖 common.logger，模块层反向依赖会成环
        from src.core.config.schema import LogConfig
        config = LogConfig()
    # trace 控制台出口和 structlog 渲染器共用同一份时间戳格式，行首风格保持一致。
    _trace_date_format = config.date_format
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



class _DynamicLogger:
    """在每次写日志时解析 structlog 配置的轻量代理。

    YueLiBot 的业务模块在 ``main`` 初始化配置前就会被 Python 导入。若直接返回
    ``structlog.get_logger().bind(...)``，该代理会在第一次绑定时冻结默认渲染器，
    导致启动后同时出现旧式 structlog 行和新的中文行。这里只保存模块名，真正的
    BoundLogger 延迟到调用 ``info``/``warning`` 等方法时取得，因此重载配置后所有
    模块仍共用同一套控制台、文件和 WebUI 处理器。
    """

    __slots__ = ('_name',)

    def __init__(self, name: str) -> None:
        """保存模块名，不触发 structlog 解析。"""

        self._name = name

    def __getattr__(self, method_name: str) -> Any:
        """把 structlog 的日志方法动态转发到当前配置。

        ``getattr`` 只用于适配 structlog 的动态日志 API；业务代码仍通过明确的
        ``logger.info``、``logger.warning`` 等属性调用，不依赖字符串兜底。
        """

        bound = structlog.get_logger().bind(logger=self._name)
        return getattr(bound, method_name)


def get_logger(name: str) -> _DynamicLogger:
    """返回不会冻结初始化时渲染器的模块日志代理。

    :param name: 要写入结构化日志的模块名。
    :return: 延迟绑定模块名的日志代理。
    """

    return _DynamicLogger(name)
