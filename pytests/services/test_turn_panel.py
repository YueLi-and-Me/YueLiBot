"""分层回合面板回归：各级模型调用要在终端上逐级可见。

面板是多级 Agent 唯一的「当场」观测入口——落盘记录是事后查的。这里盯住：
每一级各自成面板、正文与推理不截断、工具参数逐行可读、工具结果完整且按顺序
排在两次决策之间、不出回复面板的回合仍补出面板、回合外调用独立展示、输出不
重复渲染、stdout 不着色时照常输出。
"""

from __future__ import annotations

import re

import src.core.services.console.trace_console as console
from src.core.services.console.turn_panel import (
    ModelCall,
    ToolRun,
    begin_turn,
    note_model_call,
    note_tool_run,
    render_stage_panel,
    render_timing_footer,
    take_steps,
)

_ANSI = re.compile(r'\x1b\[[0-9;]*m')


def _planner_call(**overrides) -> ModelCall:
    base = dict(
        task='planner',
        model='gemini-3.7-flash',
        provider='RinkoAI',
        first_token_ms=5594,
        total_ms=8186,
        reasoning='他说困了还要抱抱，接着损两句比较像她。',
        tool_calls=[{
            'id': 'call_1',
            'name': 'reply',
            'arguments': '{"target": 3, "length": "brief", "reference": "接着损"}',
        }],
        record_path='data/logs/prompt/planner/x.json',
    )
    base.update(overrides)
    return ModelCall(**base)


def _replyer_call(**overrides) -> ModelCall:
    base = dict(
        task='replyer',
        model='gemini-2.5-pro',
        provider='RinkoAI',
        first_token_ms=7219,
        total_ms=7484,
        text='都快天亮了还算睡觉呀',
        record_path='data/logs/prompt/replyer/y.json',
    )
    base.update(overrides)
    return ModelCall(**base)


def test_turn_panel_shows_每一级(capsys) -> None:
    """决策与回复生成各自成面板，模型、耗时、记录路径与思考都在。"""
    console.mark_turn_start(9)
    note_model_call(_planner_call())
    note_model_call(_replyer_call())

    console.render_turn(
        9, '凌白', '困了，抱抱',
        messages=[{'role': 'user', 'content': 'x'}],
        reply_segments=['都快天亮了还算睡觉呀'],
        side_effects=[],
        bot_name='月璃',
        source_label='群聊·629201002',
        model_name='gemini-2.5-pro',
    )

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '决策' in out and '回复生成' in out
    assert 'gemini-3.7-flash' in out and 'gemini-2.5-pro' in out
    assert '首字 5.59 s / 共 8.19 s' in out
    assert 'data/logs/prompt/planner/x.json' in out
    assert '他说困了还要抱抱' in out
    # 页脚按级汇总耗时。
    assert '决策 8.19 s' in out and '回复生成 7.48 s' in out


def test_tool_arguments_are_readable(capsys) -> None:
    """工具参数逐行展开；挤成一行的 JSON 在终端里读不了，而它是决策层唯一的产出。"""
    console.mark_turn_start(10)
    note_model_call(_planner_call())

    console.render_turn(
        10, '凌白', '困了', messages=[], reply_segments=[], side_effects=[],
        bot_name='月璃',
        source_label='群聊·629201002',
    )

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '工具 · reply' in out
    assert '"target": 3' in out
    assert '"reference": "接着损"' in out


def test_reply_not_rendered_twice(capsys) -> None:
    """最后一级的「输出」就是那句话，不该再挂一个「模型返回」显示第二遍。"""
    console.mark_turn_start(11)
    note_model_call(_replyer_call(text='只说一遍'))

    console.render_turn(
        11, '凌白', '在吗', messages=[], reply_segments=['只说一遍'],
        side_effects=[], bot_name='月璃', source_label='私聊·10001',
    )

    out = _ANSI.sub('', capsys.readouterr().out)
    assert out.count('只说一遍') == 1
    assert '模型返回' not in out


def test_calls_outside_a_turn_request_standalone_render() -> None:
    """主动消息、日程与摘要不属于用户回合，返回 False 交给独立展示出口。"""
    take_steps()
    captured = note_model_call(_planner_call())

    assert captured is False
    assert take_steps() == []


def test_take_steps_clears_collector() -> None:
    """取走即清空，否则同一批调用会被下一轮的面板再渲染一遍。"""
    begin_turn()
    note_model_call(_planner_call())

    assert len(take_steps()) == 1
    assert take_steps() == []


def test_late_background_call_is_not_lost() -> None:
    """继承旧 ContextVar 的后台任务在轮末之后必须改走独立展示，不能落进死列表。"""
    begin_turn()
    assert note_model_call(_planner_call()) is True
    assert len(take_steps()) == 1

    assert note_model_call(_replyer_call()) is False
    assert take_steps() == []


def test_stage_panel_keeps_complete_reasoning_and_text(capsys) -> None:
    """控制台展示不再按 1200/600 字截断，末尾证据必须仍然可见。"""
    reasoning = '推' * 1400 + '【推理末尾】'
    text = '答' * 800 + '【正文末尾】'

    console.render_model_call(_replyer_call(reasoning=reasoning, text=text))

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '【推理末尾】' in out
    assert '【正文末尾】' in out


def test_background_vision_response_is_rendered_completely(capsys) -> None:
    """视觉理解等回合外调用要独立成框，并完整展示推理与描述正文。"""
    reasoning = '逐块检查画面。' * 180 + '【视觉推理末尾】'
    text = '图片里有人在桌前使用电脑。' * 80 + '【视觉描述末尾】'

    console.render_model_call(ModelCall(
        task='vision',
        model='gemini-3.5-flash-preview',
        provider='RinkoAI',
        first_token_ms=120,
        total_ms=800,
        reasoning=reasoning,
        text=text,
    ))

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '视觉理解' in out
    assert '【视觉推理末尾】' in out
    assert '【视觉描述末尾】' in out


def test_capture_console_preserves_color_when_parent_sets_no_color(monkeypatch) -> None:
    """Electron 已强制着色时，父进程的 NO_COLOR 不能再次剥掉面板色码。"""
    monkeypatch.setenv('NO_COLOR', '1')

    with console._capture_console.capture() as capture:
        console._capture_console.print(render_stage_panel(_replyer_call()), crop=False)

    output = capture.get()
    assert '\x1b[' in output
    assert console._capture_console.no_color is False


def test_failed_call_is_marked() -> None:
    """失败的那一级要在面板上自己说出来，不能只体现为「这一轮没说话」。"""
    panel = render_stage_panel(_planner_call(error='quota：额度用完了'))

    assert panel.border_style == 'red'


def test_footer_empty_without_calls() -> None:
    """没有分级调用时页脚为空，交回旧的合计耗时展示。"""
    assert render_timing_footer([]).plain == ''


def test_failure_panel_tells_what_to_do(capsys) -> None:
    """失败面板要说清三件事：哪一级挂了、什么原因、去哪看完整请求。

    只报一个 quota 类别没有用——看到它还得自己猜是余额、免费额度还是限流；
    存档路径则是复现这次调用的唯一入口。
    """
    console.mark_turn_start(12)
    note_model_call(_planner_call(
        error='quota：额度用完了',
        error_kind='quota',
        record_path='data/logs/prompt/planner/failed.json',
    ))

    console.render_turn(
        12, '凌白', '在吗', messages=[], reply_segments=[], side_effects=[],
        bot_name='月璃', source_label='私聊·10001',
    )

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '额度用完了' in out
    assert '余额不足' in out          # 类别翻成了可照做的说明
    assert 'planner/failed.json' in out


def test_same_sender_has_distinct_group_and_direct_titles(capsys) -> None:
    """同一个人的人物标签不变时，标题仍能一眼区分群聊与私聊。"""

    for turn, source in ((13, '群聊·629201002'), (14, '私聊·同一个人')):
        console.mark_turn_start(turn)
        console.render_turn(
            turn,
            '同一个人（QQ号：10001）',
            '同一句话',
            messages=[],
            reply_segments=[],
            side_effects=[],
            bot_name='月璃',
            source_label=source,
        )

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '第 13 轮 · 群聊·629201002 · 同一个人（QQ号：10001）' in out
    assert '第 14 轮 · 私聊·同一个人 · 同一个人（QQ号：10001）' in out
    assert '收到消息  [群聊·629201002] 同一个人（QQ号：10001）：同一句话' in out
    assert '收到消息  [私聊·同一个人] 同一个人（QQ号：10001）：同一句话' in out


def test_observation_and_error_include_source(capsys) -> None:
    """旁观记录与失败轮次不能掉回只显示人物的旧路径。"""

    console.render_observation(
        '群名片（QQ昵称：昵称 · QQ号：10001）',
        '没叫她',
        'attention_filtered',
        source_label='群聊·629201002',
    )
    console.mark_turn_start(15)
    console.render_turn_error(
        15,
        '昵称（QQ号：10001）',
        '在吗',
        'timeout',
        '超时',
        source_label='私聊·10001',
    )

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '旁听  [群聊·629201002] 群名片（QQ昵称：昵称 · QQ号：10001）：没叫她' in out
    assert '第 15 轮 · 私聊·10001 · 昵称（QQ号：10001） · 失败' in out
    assert '收到消息  [私聊·10001] 昵称（QQ号：10001）：在吗' in out


def _consult_run(observation: str) -> ToolRun:
    return ToolRun(name='consult', argument='龙族 设定', observation=observation)


def test_tool_result_sits_between_decisions_in_full(capsys) -> None:
    """工具结果完整排在发起调用的决策与下一次决策之间，不按单行预览截断。"""
    observation = '关于「龙族 设定」，你查到这些知识：\n- ' + '设' * 900 + '\n【结果末尾】'
    console.mark_turn_start(16)
    note_model_call(_planner_call(reasoning='先查一下知识库。'))
    assert note_tool_run(_consult_run(observation)) is True
    note_model_call(_planner_call(reasoning='查到的内容和龙族无关。'))
    note_model_call(_replyer_call())

    console.render_turn(
        16, '凌白', '知识库里有龙族吗', messages=[], reply_segments=['都快天亮了还算睡觉呀'],
        side_effects=[], bot_name='月璃', source_label='群聊·629201002',
    )

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '工具结果 · 查知识资料（consult）' in out
    assert '入参：龙族 设定' in out
    assert '【结果末尾】' in out
    assert out.index('先查一下知识库') < out.index('工具结果') < out.index('查到的内容和龙族无关')
    # 页脚只汇总模型调用耗时，工具执行不占位。
    assert '决策 8.19 s | 决策 8.19 s | 回复生成 7.48 s' in out


def test_quiet_turn_flushes_collected_steps(capsys) -> None:
    """沉默等不出回复面板的回合，决策与工具结果照样成面板，且收集随之关闭。"""
    console.mark_turn_start(17)
    note_model_call(_planner_call())
    note_tool_run(_consult_run('你查了查自己知道的东西，没有找到和「龙族」有关的知识。'))

    console.render_quiet_turn(17, '凌白', '嗯', source_label='群聊·629201002', interrupted=False)

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '第 17 轮 · 群聊·629201002 · 凌白 · 未发言' in out
    assert '没有找到和「龙族」有关的知识' in out
    # 收集已关闭：回合收尾后迟到的后台调用改走独立出口，不落进无人读取的缓冲。
    assert note_model_call(_replyer_call()) is False
    assert 17 not in console._starts


def test_quiet_turn_is_noop_after_panel_or_without_steps(capsys) -> None:
    """已出过回复面板，或回合内没有任何模型调用时，不再补面板。"""
    console.mark_turn_start(18)
    note_model_call(_replyer_call(text='只说一遍'))
    console.render_turn(
        18, '凌白', '在吗', messages=[], reply_segments=['只说一遍'],
        side_effects=[], bot_name='月璃', source_label='私聊·10001',
    )
    capsys.readouterr()
    console.render_quiet_turn(18, '凌白', '在吗', source_label='私聊·10001', interrupted=False)

    console.mark_turn_start(19)
    console.render_quiet_turn(19, '凌白', '在吗', source_label='私聊·10001', interrupted=True)

    assert capsys.readouterr().out == ''
    assert 19 not in console._starts


def test_plain_stdout_keeps_webui_colored(monkeypatch, capsys) -> None:
    """stdout 不着色时面板照常输出纯文本；WebUI 日志面板一侧仍带颜色。"""
    published: list[str] = []
    monkeypatch.setattr(console, 'is_color_enabled', lambda: False)
    monkeypatch.setattr(console.webui_logs, 'publish', published.append)

    console.render_model_call(_replyer_call(text='无头部署也看得见'))

    out = capsys.readouterr().out
    assert '无头部署也看得见' in out
    assert '\x1b[' not in out
    assert published and '\x1b[' in published[0]
