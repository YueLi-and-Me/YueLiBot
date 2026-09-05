"""控制台日志中文映射与分层渲染测试。"""

from src.core.logging.log_display import display_value, event_label, field_label
from src.core.logging.logger import ModuleColoredConsoleRenderer
from src.core.logging.logger_colors import (
    EVENT_COLOR,
    FIELD_LABEL_COLOR,
    FIELD_VALUE_COLOR,
    MODULE_COLORS,
    RESET_COLOR,
    module_color,
)


def test_log_display_translates_protocol_identifiers_without_losing_unknown_values() -> None:
    """已登记的事件、字段和枚举显示中文，未知扩展值保持可诊断。"""
    assert event_label("action_decision") == "行动决策"
    assert field_label("reasonCodes") == "决策理由"
    assert display_value(["direct_question", "topic_continuation"]) == "明确提问、延续当前话题"
    assert event_label("future_event") == "future_event"
    assert field_label("futureField") == "futureField"


def test_console_renderer_separates_chinese_fields_and_keeps_body_high_contrast() -> None:
    """控制台正文使用中文分段，追踪模块不再把整行染成低亮灰色。"""
    renderer = ModuleColoredConsoleRenderer(colors=False)
    rendered = renderer(None, "info", {
        "timestamp": "08-18 10:20:30",
        "level": "info",
        "logger": "src.core.observe.events",
        "event": "trace",
        "kind": "action_decision",
        "turnId": 42,
        "action": "reply",
        "reasonCodes": ["direct_question", "topic_continuation"],
        "accepted": True,
    })

    assert rendered == (
        "08-18 10:20:30 [追踪] 运行追踪 "
        "事件类型：行动决策 │ 轮次：42 │ 动作：回复 │ "
        "决策理由：明确提问、延续当前话题 │ 已接收：是"
    )


def test_console_renderer_uses_independent_colors_for_module_event_and_fields() -> None:
    """彩色输出的模块、事件、字段和值使用独立 ANSI 片段。"""
    renderer = ModuleColoredConsoleRenderer(colors=True)
    rendered = renderer(None, "warning", {
        "timestamp": "08-18 10:20:30",
        "level": "warning",
        "logger": "src.core.observe.events",
        "event": "llm_error",
        "errorKind": "timeout",
    })

    assert MODULE_COLORS["observe.events"][0] != "#6c6c6c"
    assert f"{module_color('observe.events')}[追踪]{RESET_COLOR}" in rendered
    assert f"{EVENT_COLOR}模型调用失败{RESET_COLOR}" in rendered
    assert f"{FIELD_LABEL_COLOR}错误类型{RESET_COLOR}" in rendered
    assert f"{FIELD_VALUE_COLOR}超时{RESET_COLOR}" in rendered
