"""把每轮对话合成一个嵌套彩色面板，并同步呈现到终端、Electron 控制台与 WebUI 日志面板。

完整提示词与模型原始响应仍由观察事件账本记录，本模块只负责面向人的摘要展示：轮次头、
「模型请求」「模型返回」「内部变化」子面板与底部耗时页脚。渲染出的 rich 面板被捕获为带
ANSI 的字符串后一次写两处——``stdout``（本机终端与 Electron 控制台）和 ``webui_logs``
（WebUI 日志面板），因此三端呈现一致。终端能力判断复用 ``common.logger_colors``：非彩色/
非交互场景（如重定向到文件或测试）所有渲染函数保持无操作，避免把调试面板混入服务日志。
"""

from __future__ import annotations

from typing import Any
import sys
import time

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from src.core.common.log_display import event_label, value_label
from src.core.services.turn_panel import (
    begin_turn as begin_turn_capture,
    render_stage_panel,
    render_timing_footer,
    take_calls,
)
from src.core.common.logger import get_logger
from src.core.common.logger_colors import is_color_enabled
from src.core.webui.logs import webui_logs

logger = get_logger(__name__)

# 面板固定外框宽度。固定值让终端、Electron 控制台与 WebUI 面板三端换行完全一致，
# 也避免 rich 因 stdout 是管道而回退到窄默认宽度导致排版跳动；取 100 落在既有纯文本
# 框宽度区间内（启动公告 76+、管线追踪框 48–118）。
_PANEL_WIDTH = 100

# 是否渲染面板。import 期一次性判断：Electron 启动 Python 时注入 YUELI_FORCE_COLOR=1，
# 故管道转发场景同样为真；重定向到文件或 pytest 下为假，渲染函数直接跳过。
_render_enabled = is_color_enabled()

# 捕获用 Console：force_terminal 让 rich 即便面对管道/捕获缓冲也输出 ANSI；truecolor 保证
# 24 位色；固定 width 产出确定性排版。它只用于 capture()，不直接持有 stdout。
#
# safe_box=False 关闭 rich 面向旧版 Windows 终端的 ASCII 盒线替换：
# - 现象：默认 safe_box 会把圆角盒线 ╭╮╰╯ 换成 +-|，与既有 render_box 风格不一致。
# - 原因：rich 按平台探测是否替换，与真实 stdout 编码无关；捕获场景也会命中替换。
# - 后果：本机终端、Electron 控制台、WebUI 面板均支持 UTF-8 盒线（Electron 侧注入
#   PYTHONIOENCODING=utf-8），关闭替换才能得到与截图一致的圆角嵌套盒线。
_capture_console = Console(
    force_terminal=True,
    color_system="truecolor",
    width=_PANEL_WIDTH,
    legacy_windows=False,
    safe_box=False,
)

_starts: dict[int, float] = {}


def _emit_console_block(renderable: RenderableType) -> None:
    """把一个 rich 可渲染对象一次写到 stdout 与 WebUI 日志面板。

    :param renderable: 已组装好的面板或文本。

    副作用：
        向标准输出写入带 ANSI 的多行文本，并把同一段文本发布到 ``webui_logs``。
        用 capture() 而非直接 print，确保两处拿到的是同一份确定性 ANSI 输出。
    """

    with _capture_console.capture() as capture:
        _capture_console.print(renderable)
    text = capture.get()
    # stdout 保留结尾换行让相邻面板留白；WebUI 面板按整段发布，去掉尾换行避免多出空行。
    print(text, end="", flush=True)
    webui_logs.publish(text.rstrip("\n"))


def mark_turn_start(turn: int) -> None:
    """记录对话回合开始时间，并开启本回合的模型调用收集。

    :param turn: 对话回合 ID。

    副作用：非渲染场景不写入计时表，因为后续渲染也不会发生；渲染场景同时把
        当前协程上下文标记为「回合内」，此后各级模型调用都会进本回合的面板。
    """

    if not _render_enabled:
        return
    _starts[turn] = time.monotonic()
    begin_turn_capture()


def _elapsed_ms(turn: int) -> str:
    """取出回合开始时间并格式化耗时。

    :param turn: 对话回合 ID。

    :return: 以毫秒表示的耗时文本；没有开始记录时返回 ``—``。

    副作用：消费并删除该回合的开始时间记录。
    """

    started = _starts.pop(turn, None)
    if started is None:
        return '—'
    return f'{(time.monotonic() - started) * 1000:.0f} ms'


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


def _request_panel(messages: list[dict], model_name: str, *, border_style: str = 'green') -> Panel:
    """构建「模型请求」子面板：只列模型名与上下文规模，完整提示词指向观察面板。

    :param messages: 发送给模型的消息列表，仅用于统计条数。
    :param model_name: 请求所用模型名；为空时省略该行。
    :param border_style: 子面板边框色，默认 ``green``；失败轮次传红色系。

    :return: 组装好的「模型请求」面板。
    """

    lines: list[str] = []
    normalized_model = model_name.strip()
    if normalized_model:
        lines.append(f'请求模型：{normalized_model}')
    # 失败轮次不带消息列表，只展示模型名，避免出现误导性的「上下文消息：0 条」。
    if messages:
        lines.append(f'上下文消息：{len(messages)} 条（完整提示词见观察面板）')
    return Panel(Text('\n'.join(lines)), title='模型请求', border_style=border_style, padding=(0, 1))


def render_turn(
    turn: int,
    sender_label: str,
    user_text: str,
    messages: list[dict],
    reply_segments: list[str],
    side_effects: list[dict],
    bot_name: str,
    *,
    model_name: str = '',
) -> None:
    """把一轮对话合成一个嵌套面板并三端呈现。

    :param turn: 对话回合 ID。
    :param sender_label: 发送者展示名。
    :param user_text: 用户原始文本。
    :param messages: 发送给模型的消息列表，仅展示条数。
    :param reply_segments: 解析器按 ``<say>`` 边界切分出的出站分句；模型原始输出（含协议
        标签）由事件账本保留，控制台只展示剥掉标签后的可见正文。
    :param side_effects: 本轮解析出的副作用列表。
    :param bot_name: 主体展示名。
    :param model_name: 本轮请求所用模型名；为空时「模型请求」面板省略该行。

    副作用：
        向三端写入嵌套面板；面板渲染异常只记录调试日志，不影响聊天主流程。
    """
    if not _render_enabled:
        return
    try:
        # 各级模型调用（决策 / 回复生成 / 认知检索）逐级成面板；拿不到时说明
        # 本轮没经过路由层收集，退回只列模型名与上下文规模的旧摘要。
        stage_calls = take_calls()
        stage_panels: list[Any] = (
            [render_stage_panel(call) for call in stage_calls]
            if stage_calls
            else [_request_panel(messages, model_name)]
        )
        children: list[Any] = [
            Text.assemble(
                Text('收到消息  ', style='bold cyan'),
                Text(f'{sender_label}：{user_text}', style='bold white'),
            ),
            *stage_panels,
        ]
        reply = _reply_text(reply_segments)
        # 分级面板里最后一级的「输出」已经是这句话；再挂一个「模型返回」等于同一句
        # 在同一个框里显示两遍，扫读时反而要多确认一次是不是发了两条。
        if stage_calls:
            pass
        elif reply:
            children.append(
                Panel(
                    Text(f'{bot_name}：{reply}', style='bright_green'),
                    title='模型返回', border_style='green', padding=(0, 1),
                )
            )
        else:
            children.append(
                Panel(
                    Text(f'{bot_name}：本轮没有可见回复，仅处理内部事件', style='italic yellow'),
                    title='模型返回', border_style='green', padding=(0, 1),
                )
            )
        # 副作用仅在存在时才单独出面板，避免每轮都挂一个空的「内部变化」框。
        effect_lines = _side_effect_lines(side_effects)
        if effect_lines:
            children.append(
                Panel(
                    Text('\n'.join(effect_lines), style='bright_yellow'),
                    title='内部变化', border_style='yellow', padding=(0, 1),
                )
            )
        footer = render_timing_footer(stage_calls)
        subtitle = (
            f'{footer.plain} | 合计 {_elapsed_ms(turn)}'
            if footer.plain else f'耗时 {_elapsed_ms(turn)}'
        )
        _emit_console_block(Panel(
            Group(*children),
            title=f'第 {turn} 轮 · {sender_label}',
            subtitle=subtitle,
            border_style='bright_cyan',
            padding=(0, 1),
        ))
    except Exception as exc:
        logger.debug('render_turn_failed', error=str(exc))


def render_observation(sender_label: str, user_text: str, reason: str) -> None:
    """以单行显示被回复门控拦截的群消息。

    :param sender_label: 发送者展示名。
    :param user_text: 用户原始文本。
    :param reason: 未回复的机器可读或可读原因。

    副作用：
        向三端写入观察行；渲染异常只记录调试日志。
    """
    if not _render_enabled:
        return
    try:
        _emit_console_block(
            Text('旁听  ', style='bold magenta')
            + Text(f'{sender_label}：', style='bold cyan')
            + Text(user_text, style='white')
            + Text(f'  未回复：{value_label(reason)}', style='yellow'),
        )
    except Exception as exc:
        logger.debug('render_observation_failed', error=str(exc))


# 控制台里观察结果只展示开头：完整正文在事件账本里，终端要的是一眼可读。
_OBSERVATION_CONSOLE_CHARS = 60


def _clip_observation(text: str) -> str:
    """把观察结果压成单行并截断到控制台可读长度。"""
    single_line = ' '.join(text.split())
    if len(single_line) <= _OBSERVATION_CONSOLE_CHARS:
        return single_line
    return f'{single_line[:_OBSERVATION_CONSOLE_CHARS]}…'


def render_action_decision(
    turn: int,
    agent_scope: str,
    event_status: str,
    detail: str = "",
    action: str = "",
    reason_codes: tuple[str, ...] | list[str] = (),
    target_message_ids: tuple[int, ...] | list[int] = (),
    query: str = "",
    observation: str = "",
) -> None:
    """以单行渲染 Conversation Agent 的行动决策摘要。

    该入口只做观察展示，不触碰事件账本与业务行为；非渲染场景自动跳过，
    避免把决策行混入服务日志。

    :param turn: 对话回合 ID。
    :param agent_scope: Agent 灰度路径标识，例如 shadow。
    :param event_status: action_decision 的事件状态。
    :param query: 认知动作的检索词；终局动作为空。
    :param observation: 认知动作的观察结果摘要；终局动作为空。
    :param detail: 失败状态的人类可读原因；成功状态通常为空。
    :param action: 已提交决策的动作名；失败状态为空。
    :param reason_codes: 已提交决策的理由码。
    :param target_message_ids: 已提交决策的目标消息 ID。

    副作用：
        向三端写入一行决策摘要；渲染异常只记录调试日志。
    """
    if not _render_enabled:
        return
    try:
        scope_label = '影子观察' if agent_scope == 'shadow' else value_label(agent_scope)
        parts: list[Any] = [
            Text('行动决策  ', style='bold magenta'),
            Text(f'{scope_label} · 第 {turn} 轮', style='bold cyan'),
        ]
        if action:
            summary = f'  动作：{value_label(action)}'
            if query:
                summary += f'  查：{query}'
            if reason_codes:
                summary += f"  理由：{'、'.join(value_label(code) for code in reason_codes)}"
            if target_message_ids:
                summary += f"  目标消息：{'、'.join(str(target) for target in target_message_ids)}"
            if observation:
                # 观察正文可能很长，控制台只给一眼能看完的开头。
                summary += f'  结果：{_clip_observation(observation)}'
            # 会产出可见内容的动作用绿色，其余（沉默、等待、认知轮）用黄色，
            # 一眼就能在滚动的日志里分出「她说话了」和「她没说话」。
            style = 'green' if action in ('reply', 'speak') else 'yellow'
            parts.append(Text(summary, style=style))
        else:
            summary = f'  状态：{value_label(event_status)}'
            if detail:
                summary += f'  说明：{detail}'
            parts.append(Text(summary, style='bold red'))
        _emit_console_block(Text.assemble(*parts))
    except Exception as exc:
        logger.debug('render_action_decision_failed', error=str(exc))


def render_turn_error(
    turn: int,
    sender_label: str,
    user_text: str,
    kind: str,
    message: str,
    *,
    model_name: str = '',
) -> None:
    """把一轮失败对话合成红色面板并三端呈现。

    :param turn: 对话回合 ID。
    :param sender_label: 发送者展示名。
    :param user_text: 用户原始文本。
    :param kind: 错误类别。
    :param message: 错误消息。
    :param model_name: 本轮请求所用模型名；为空时省略「模型请求」面板。

    副作用：
        向三端写入错误面板；渲染异常只记录调试日志。
    """

    if not _render_enabled:
        return
    try:
        children: list[Any] = [
            Text.assemble(
                Text('收到消息  ', style='bold cyan'),
                Text(f'{sender_label}：{user_text}', style='bold white'),
            ),
        ]
        if model_name.strip():
            children.append(_request_panel([], model_name, border_style='red'))
        children.append(Panel(
            Text(f'错误类型：{event_label(kind)}\n错误信息：{message}', style='bold red'),
            title='错误', border_style='red', padding=(0, 1),
        ))
        _emit_console_block(Panel(
            Group(*children),
            title=f'第 {turn} 轮 · {sender_label} · 失败',
            subtitle=f'耗时 {_elapsed_ms(turn)}',
            border_style='bright_red',
            padding=(0, 1),
        ))
    except Exception as exc:
        logger.debug('render_turn_error_failed', error=str(exc))
