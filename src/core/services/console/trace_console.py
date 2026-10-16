"""把每轮对话合成一个嵌套彩色面板，并同步呈现到终端、Electron 控制台与 WebUI 日志面板。

完整提示词仍由观察记录保存；模型返回则在这里把正文、推理、工具调用与工具结果完整展示。
属于用户回合的调用在轮末嵌套面板中呈现，视觉理解、摘要、记忆抽取、日程等回合外调用在
完成时独立成框。渲染出的 rich 面板被捕获为带 ANSI 的字符串后一次写两处——``stdout``
（本机终端、Electron 控制台与 systemd journal）和 ``webui_logs``（WebUI 日志面板）。
WebUI 一侧始终带颜色；``stdout`` 是否带颜色由 ``logging.logger_colors`` 的全进程裁定决定，
不着色时写入去掉 ANSI 的纯文本。是否渲染与是否着色无关：无头部署时 WebUI 就是控制台。
"""

from __future__ import annotations

from typing import Any
import re
import time

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from src.core.logging.log_display import value_label
from src.core.logging.logger import get_logger
from src.core.logging.logger_colors import is_color_enabled
from src.core.services.console.turn_panel import (
    ModelCall,
    ToolRun,
    begin_turn as begin_turn_capture,
    render_stage_panel,
    render_step_panel,
    render_timing_footer,
    render_tool_panel,
    take_steps,
)
from src.core.webui.logs import webui_logs

logger = get_logger(__name__)

# 面板固定外框宽度。固定值让终端、Electron 控制台与 WebUI 面板三端换行完全一致，
# 也避免 rich 因 stdout 是管道而回退到窄默认宽度导致排版跳动；取 100 落在既有纯文本
# 框宽度区间内（启动公告 76+、管线追踪框 48–118）。
_PANEL_WIDTH = 100
# Rich 会把嵌套 Panel 的可用高度继承为当前终端行数；未显式提供高度时，长推理即使
# 没有字符截断也会在约 25 行处被裁掉。这里给捕获控制台一个只用于布局的极大高度，
# 最终字符串仍只包含实际内容，不会补齐空行；它不是业务长度上限。
_PANEL_LAYOUT_HEIGHT = 1_000_000

# 捕获结果中的 ANSI 控制序列（CSI），用于向不着色的 stdout 写纯文本。
_ANSI_SEQUENCE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')

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
    # 捕获结果始终带色：WebUI 一侧恒定着色，stdout 一侧是否去色由 ``is_color_enabled``
    # 统一裁定。Rich 若再次读取父进程的 NO_COLOR，会覆盖 Electron 注入的
    # YUELI_FORCE_COLOR=1，出现面板生成成功却没有 ANSI 色码的矛盾；这里禁止二次环境裁定。
    no_color=False,
    width=_PANEL_WIDTH,
    height=_PANEL_LAYOUT_HEIGHT,
    legacy_windows=False,
    safe_box=False,
)

_starts: dict[int, float] = {}


def _emit_console_block(renderable: RenderableType) -> None:
    """把一个 rich 可渲染对象一次写到 stdout 与 WebUI 日志面板。

    :param renderable: 已组装好的面板或文本。

    副作用：
        向标准输出写入多行文本，并把同一段带 ANSI 的文本发布到 ``webui_logs``。
        用 capture() 而非直接 print，确保两处拿到的是同一份确定性排版；全进程
        裁定为不着色时，stdout 一侧去掉 ANSI 序列后再写。

    渲染与否不得与着色裁定绑定。

    - 现象：服务器上 systemd 托管时 stdout 不是 TTY，回合面板、行动决策行与回合外
      模型调用面板在 journal 与 WebUI 日志面板里全部缺失，只剩门控等运行追踪框。
    - 原因：此前以 ``is_color_enabled()`` 作为渲染开关，非 TTY 即整体跳过；而 WebUI
      日志支路按设计独立于终端能力（见 ``logger.WebUiLogHandler``）。
    - 后果：恢复该绑定会使无头部署下 WebUI 日志面板再次看不到模型输出与工具结果。
    """

    with _capture_console.capture() as capture:
        # crop=False 同时关闭最外层终端裁剪；嵌套面板的高度由上面的布局高度保证。
        _capture_console.print(renderable, crop=False)
    text = capture.get()
    # stdout 保留结尾换行让相邻面板留白；WebUI 面板按整段发布，去掉尾换行避免多出空行。
    print(text if is_color_enabled() else _ANSI_SEQUENCE.sub('', text), end="", flush=True)
    webui_logs.publish(text.rstrip("\n"))


def render_model_call(call: ModelCall) -> None:
    """独立展示一次不属于活跃用户回合的完整模型返回。

    视觉理解通常在批次真正开始前完成；摘要、事实抽取、表达学习和场景观察则在
    回合收尾后后台执行。它们都经过统一模型路由，却没有用户回合面板可以承载，
    因而必须在调用完成时直接成框，否则控制台只会看到“请求模型”而看不到结果。

    :param call: 路由层已经聚合完成的模型调用，包含正文、推理、工具调用与错误。

    副作用：
        向终端、Electron 控制台与 WebUI 日志面板写入同一份完整调用面板；渲染
        失败只记录调试日志，不影响模型调用结果。
    """

    try:
        _emit_console_block(render_stage_panel(call))
    except Exception as exc:
        logger.debug('render_model_call_failed', task=call.task, error=str(exc))


def render_tool_run(run: ToolRun) -> None:
    """独立展示一次不属于活跃用户回合的工具执行结果。

    :param run: 工具名、入参与完整结果。

    副作用：
        向三端写入工具结果面板；渲染失败只记录调试日志。
    """

    try:
        _emit_console_block(render_tool_panel(run))
    except Exception as exc:
        logger.debug('render_tool_run_failed', tool=run.name, error=str(exc))


def mark_turn_start(turn: int) -> None:
    """记录对话回合开始时间，并开启本回合的模型调用收集。

    :param turn: 对话回合 ID。

    副作用：写入计时表，并把当前协程上下文标记为「回合内」，此后各级模型调用
        与工具执行都会进本回合的面板。计时记录由回合面板渲染时取走。
    """

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
    """将记忆、情绪、约定和表情包副作用转换为面板行文本。

    :param side_effects: 解析事件产生的副作用字典列表。

    :return: 当前支持的 ``mood_delta``、``promise_stashed``、``emoji_selected``
        与 ``emoji_selection_missed`` 副作用行；未知类型被忽略。
        记忆写入不在其中——它已改由回合之后的后台抽取产出独立的 ``memory_fact``
        观察事件，不再经过本轮的副作用 sink。
    """

    lines = []
    for effect in side_effects:
        if effect.get('kind') == 'mood_delta':
            lines.append(
                f"心情：好感 {effect.get('favor')}，精力 {effect.get('energy')}"
            )
        elif effect.get('kind') == 'promise_stashed':
            lines.append(f"约定：{effect.get('subject', '')} → {effect.get('at')}")
        elif effect.get('kind') == 'emoji_selected':
            lines.append(
                f"表情包：{effect.get('hash', '')} · 目标情绪 {effect.get('emotion', '')}"
                f" · 标签 {effect.get('tags', '')}"
                f" · 候选 {effect.get('candidateCount', '未知')} 张"
            )
        elif effect.get('kind') == 'emoji_selection_missed':
            lines.append(
                f"表情包落空：目标情绪 {effect.get('emotion', '')}，"
                "写了 <emoji> 但库里没命中"
            )
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
    source_label: str,
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
    :param source_label: 当前消息所在 stream 的可读来源标签，与发送者标签分开呈现。
    :param model_name: 本轮请求所用模型名；为空时「模型请求」面板省略该行。

    副作用：
        向三端写入嵌套面板；面板渲染异常只记录调试日志，不影响聊天主流程。
    """
    try:
        # 各级模型调用（决策 / 回复生成）与其间的工具结果按发生顺序逐级成面板；
        # 拿不到时说明本轮没经过路由层收集，退回只列模型名与上下文规模的旧摘要。
        stage_steps = take_steps()
        has_model_calls = any(isinstance(step, ModelCall) for step in stage_steps)
        stage_panels: list[Any] = [render_step_panel(step) for step in stage_steps]
        if not has_model_calls:
            stage_panels.insert(0, _request_panel(messages, model_name))
        children: list[Any] = [
            _received_line(source_label, sender_label, user_text),
            *stage_panels,
        ]
        reply = _reply_text(reply_segments)
        # 分级面板里最后一级的「输出」已经是这句话；再挂一个「模型返回」会把
        # 同一内容在同一框内显示两遍。
        if has_model_calls:
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
        _emit_console_block(Panel(
            Group(*children),
            title=f'第 {turn} 轮 · {source_label} · {sender_label}',
            subtitle=_turn_subtitle(turn, stage_steps),
            border_style='bright_cyan',
            padding=(0, 1),
        ))
    except Exception as exc:
        logger.debug('render_turn_failed', error=str(exc))


def render_quiet_turn(
    turn: int,
    sender_label: str,
    user_text: str,
    *,
    source_label: str,
    interrupted: bool,
) -> None:
    """为未渲染回复面板与失败面板的回合输出面板，并结束本回合的收集。

    沉默、等下文、戳一戳、贴表情、被新消息打断、空回复与门控拦截都不调用
    :func:`render_turn`；这些回合里已经发生的决策思考、工具结果与会话印象等
    模型调用若不在这里取走，会随回合结束一起丢失，回合缓冲也会一直保持开放，
    使此后从该上下文派生的摘要与记忆抽取调用落进无人读取的列表。

    :param turn: 对话回合 ID。
    :param sender_label: 发送者展示名。
    :param user_text: 本回合批次的用户文本。
    :param source_label: 当前消息所在 stream 的可读来源标签。
    :param interrupted: 回合是否因新消息被打断；决定标题后缀。

    副作用：
        取走并关闭本回合的收集缓冲，消费计时记录；已由回复面板或失败面板取走、
        或本回合没有任何模型调用时不输出。渲染异常只记录调试日志。
    """
    stage_steps = take_steps()
    if not stage_steps:
        _starts.pop(turn, None)
        return
    try:
        suffix = '被新消息打断' if interrupted else '未发言'
        _emit_console_block(Panel(
            Group(
                _received_line(source_label, sender_label, user_text),
                *(render_step_panel(step) for step in stage_steps),
            ),
            title=f'第 {turn} 轮 · {source_label} · {sender_label} · {suffix}',
            subtitle=_turn_subtitle(turn, stage_steps),
            border_style='yellow' if interrupted else 'bright_black',
            padding=(0, 1),
        ))
    except Exception as exc:
        logger.debug('render_quiet_turn_failed', error=str(exc))


def _received_line(source_label: str, sender_label: str, user_text: str) -> Text:
    """组装回合面板首行「收到消息 [来源] 发送者：正文」。"""
    return Text.assemble(
        Text('收到消息  ', style='bold cyan'),
        Text(f'[{source_label}] ', style='bold magenta'),
        Text(f'{sender_label}：{user_text}', style='bold white'),
    )


def _turn_subtitle(turn: int, stage_steps: list[Any]) -> str:
    """组装回合面板页脚：各级模型耗时加回合合计耗时。

    :param turn: 对话回合 ID。
    :param stage_steps: 本回合已取走的各级内容。
    :return: 页脚文本。
    副作用：消费该回合的计时记录。
    """
    footer = render_timing_footer(stage_steps)
    elapsed = _elapsed_ms(turn)
    return f'{footer.plain} | 合计 {elapsed}' if footer.plain else f'耗时 {elapsed}'


def render_observation(
    sender_label: str,
    user_text: str,
    reason: str,
    *,
    source_label: str,
) -> None:
    """以单行显示被回复门控拦截的群消息。

    :param sender_label: 发送者展示名。
    :param user_text: 用户原始文本。
    :param reason: 未回复的机器可读或可读原因。
    :param source_label: 当前消息所在 stream 的可读来源标签。

    副作用：
        向三端写入观察行；渲染异常只记录调试日志。
    """
    try:
        _emit_console_block(
            Text('旁听  ', style='bold magenta')
            + Text(f'[{source_label}] ', style='bold magenta')
            + Text(f'{sender_label}：', style='bold cyan')
            + Text(user_text, style='white')
            + Text(f'  未回复：{value_label(reason)}', style='yellow'),
        )
    except Exception as exc:
        logger.debug('render_observation_failed', error=str(exc))


# 行动决策行里工具结果只展示开头（字符），作为回合进行中的实时进度；完整结果在轮末
# 回合面板的「工具结果」子面板与事件账本的 tool_execution 事件里。
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

    该入口只做观察展示，不触碰事件账本与业务行为。

    :param turn: 对话回合 ID。
    :param agent_scope: Agent 灰度路径标识，例如 shadow。
    :param event_status: action_decision 的事件状态。
    :param query: 认知动作的检索词；终局动作为空。
    :param observation: 认知动作的观察结果；单行只展示开头，终局动作为空。
    :param detail: 失败状态的人类可读原因；成功状态通常为空。
    :param action: 已提交决策的动作名；失败状态为空。
    :param reason_codes: 已提交决策的理由码。
    :param target_message_ids: 已提交决策的目标消息 ID。

    副作用：
        向三端写入一行决策摘要；渲染异常只记录调试日志。
    """
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
                # 观察正文可能很长，单行只展示开头，完整结果见轮末回合面板。
                summary += f'  结果：{_clip_observation(observation)}'
            # 会产出可见内容的动作用绿色，其余（沉默、等待、认知轮）用黄色，
            # 便于在滚动日志中区分 Bot 是否发言。
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
    source_label: str,
    model_name: str = '',
) -> None:
    """把一轮失败对话合成红色面板并三端呈现。

    :param turn: 对话回合 ID。
    :param sender_label: 发送者展示名。
    :param user_text: 用户原始文本。
    :param kind: 错误类别。
    :param message: 错误消息。
    :param source_label: 当前消息所在 stream 的可读来源标签，与发送者标签分开呈现。
    :param model_name: 本轮请求所用模型名；为空时省略「模型请求」面板。

    副作用：
        向三端写入错误面板；渲染异常只记录调试日志。
    """

    try:
        # 失败轮同样要取走本回合的调用：一是失败面板要显示挂在哪一级、存档在哪，
        # 二是不取走的话这批调用会残留到下一轮面板里，显示成那一轮多调了模型。
        stage_steps = take_steps()
        children: list[Any] = [_received_line(source_label, sender_label, user_text)]
        if stage_steps:
            children.extend(render_step_panel(step) for step in stage_steps)
        elif model_name.strip():
            children.append(_request_panel([], model_name, border_style='red'))
        children.append(Panel(
            # 失败状态是 VALUE_LABELS 里的取值而不是事件名，用 event_label 查不到，
            # 面板上会直接露出 provider_error 这样的原文。
            Text(f'错误类型：{value_label(kind)}\n错误信息：{message}', style='bold red'),
            title='错误', border_style='red', padding=(0, 1),
        ))
        _emit_console_block(Panel(
            Group(*children),
            title=f'第 {turn} 轮 · {source_label} · {sender_label} · 失败',
            subtitle=f'耗时 {_elapsed_ms(turn)}',
            border_style='bright_red',
            padding=(0, 1),
        ))
    except Exception as exc:
        logger.debug('render_turn_error_failed', error=str(exc))
