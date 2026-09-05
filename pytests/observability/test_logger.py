"""
控制台日志渲染与模块色表的测试。

覆盖率那一项是有意为之的「让错误及时完整地暴露」：色表和别名表是手工维护的，
新加一个模块很容易忘了登记。与其在渲染器里加运行时兜底把问题盖过去，不如让
这条测试红给人看 —— 见 CLAUDE.md 的 debug 规范。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Set

import ast
import re

import pytest

from src.core.logging.logger import (
    ModuleColoredConsoleRenderer,
    emit_console_trace,
    get_logger,
    initialize_logging,
)
from src.core.logging.logger_colors import (
    MODULE_ALIASES,
    MODULE_COLORS,
    hex_to_rgb,
    module_alias,
    normalize_logger_name,
    rgb_to_256_index,
)
from src.core.config.schema import LogConfig

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
# ANSI 转义序列，用来断言「该有色」和「不该有色」
_ANSI = re.compile(r"\033\[[0-9;]*m")


def _module_name_of(path: Path) -> str:
    """由文件相对 src/ 的路径推出运行时的 __name__。

    返回带 src. 前缀的完整模块路径，交由 normalize_logger_name 按与运行时
    完全相同的规则裁剪；分包之后 src.core. 是要整段剥掉的，若在此处提前去掉
    src. 会得到 core.api.http 这种运行时不存在的名字。
    """
    relative = path.relative_to(_SRC_ROOT).with_suffix("")
    parts: List[str] = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(["src", *parts])


def _declared_logger_names() -> Set[str]:
    """
    扫 src/ 下所有 get_logger 调用点，还原出它们会拿到的模块名。

    走 ast 而不是正则：文档字符串和注释里也写着 `get_logger(__name__)`，
    正则会把那些说明文字当成真调用点。
    """
    names: Set[str] = set()
    for path in _SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "get_logger"):
                continue
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                names.add(argument.value)
            elif isinstance(argument, ast.Name) and argument.id == "__name__":
                names.add(_module_name_of(path))
    return names


def test_每个模块都登记了颜色和别名() -> None:
    """新增模块忘了进色表，这条会红。"""
    declared = {normalize_logger_name(name) for name in _declared_logger_names()}
    # logger.py 自己的示例/文档字符串不该混进来，但真实调用点必须齐全
    assert declared, "没扫到任何 get_logger 调用点，正则或目录结构变了"

    assert not (declared - MODULE_COLORS.keys()), "这些模块没在 MODULE_COLORS 里登记颜色"
    assert not (declared - MODULE_ALIASES.keys()), "这些模块没在 MODULE_ALIASES 里登记中文别名"


def test_色表和别名表键一致() -> None:
    assert MODULE_COLORS.keys() == MODULE_ALIASES.keys()


def test_别名都是中文() -> None:
    """照 CLAUDE.md 的语言规范，控制台展示语言以简体中文为准。"""
    for name, alias in MODULE_ALIASES.items():
        assert any("一" <= char <= "鿿" for char in alias), f"{name} 的别名 {alias!r} 没有中文"


def test_渲染出中文别名与结构化字段() -> None:
    renderer = ModuleColoredConsoleRenderer(colors=False)
    line = renderer(None, "info", {
        "timestamp": "08-04 20:18:33",
        "level": "info",
        "logger": "src.core.services.proactive",
        "event": "awareness_started",
        "sleep": "awake",
    })
    assert line == "08-04 20:18:33 [感知] 感知服务已启动 睡眠状态：清醒"


def test_不上色时不带任何转义序列() -> None:
    renderer = ModuleColoredConsoleRenderer(colors=False)
    line = renderer(None, "warning", {
        "timestamp": "08-04 20:18:33",
        "level": "warning",
        "logger": "src.main",
        "event": "llm_init_failed",
        "error": "空的",
    })
    assert not _ANSI.search(line)


def test_上色时时间戳与模块名各自带色() -> None:
    renderer = ModuleColoredConsoleRenderer(colors=True)
    line = renderer(None, "error", {
        "timestamp": "08-04 20:18:33",
        "level": "error",
        "logger": "src.core.services.chat",
        "event": "turn_failed",
    })
    # error 级别的时间戳是红色
    assert line.startswith("\033[31m08-04 20:18:33\033[0m")
    assert "对话" in line
    assert _ANSI.search(line)


def test_中文不被转义成unicode码点() -> None:
    """dict/list 值走 json.dumps，必须 ensure_ascii=False。"""
    renderer = ModuleColoredConsoleRenderer(colors=False)
    line = renderer(None, "info", {
        "timestamp": "08-04 20:18:33",
        "level": "info",
        "logger": "src.core.services.chat",
        "event": "schedule_ready",
        "plan": {"上午": "写代码"},
    })
    assert "上午" in line and "写代码" in line
    assert "\\u" not in line


def test_异常单独换行附在后面() -> None:
    renderer = ModuleColoredConsoleRenderer(colors=False)
    line = renderer(None, "error", {
        "timestamp": "08-04 20:18:33",
        "level": "error",
        "logger": "src.main",
        "event": "boom",
        "exception": "Traceback...\nValueError",
    })
    assert line.endswith("\nTraceback...\nValueError")
    head = line.split("\n")[0]
    assert head == "08-04 20:18:33 [主程序] boom"


def test_未登记的模块原样打印不报错() -> None:
    """色表漏登记由上面的覆盖率测试负责暴露，渲染这一层不该跟着崩。"""
    renderer = ModuleColoredConsoleRenderer(colors=True)
    line = renderer(None, "info", {
        "timestamp": "08-04 20:18:33",
        "level": "info",
        "logger": "src.brand.new",
        "event": "hello",
    })
    assert "[brand.new]" in line


def test_模块名去掉src前缀() -> None:
    assert normalize_logger_name("src.core.services.media.tts") == "services.media.tts"
    assert normalize_logger_name("main") == "main"
    assert module_alias("services.media.tts") == "语音"


def test_十六进制转rgb与256色近邻() -> None:
    assert hex_to_rgb("#5f87ff") == (95, 135, 255)
    assert hex_to_rgb("#58f") == (85, 136, 255)
    # 纯黑/纯白必须落在调色板的对应格上
    assert rgb_to_256_index(0, 0, 0) == 0
    assert rgb_to_256_index(255, 255, 255) == 15


def test_日志等级写错立刻报错() -> None:
    """不静默降级成 INFO —— 配置写错了要让人当场看见。"""
    with pytest.raises(ValueError, match="未知的日志等级"):
        initialize_logging(LogConfig(level="INF0"))


def test_初始化前创建的日志代理会使用最新渲染器(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """模块级 logger 不应把启动前的 structlog 默认排版带进正式日志。"""
    monkeypatch.setenv('YUELI_FORCE_COLOR', '1')
    logger = get_logger('src.core.services.media.emoji')
    initialize_logging(LogConfig(level='INFO', color_scope='full', to_file=False))

    logger.info('emoji_integrity_verified', count=1)

    output = capsys.readouterr().out
    assert '[表情包]' in output
    assert '表情包完整性检查完成' in output
    assert '[info     ]' not in output


def test_可见性拦截即使属于当前回合也在控制台显示来源(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """可见性拦截是 W7 的目视入口，不能被回合事件静音规则吞掉。"""

    emit_console_trace({
        'seq': 8,
        'at': 1_788_400_000_000,
        'kind': 'memory_fact_scope_blocked',
        'stage': 'thinking',
        'stageLabel': '正在思考',
        'streamId': 3,
        'turnId': 8,
        'streamKind': 'group',
        'streamExternalId': '629201002',
        'sourceLabel': '群聊·629201002',
        'factOriginKind': 'direct',
        'blocked': 3,
    })

    output = _ANSI.sub('', capsys.readouterr().out)
    assert '事实被场合边界挡下' in output
    assert '事实来源：私聊' in output
    assert '会话来源：群聊·629201002' in output
    assert '会话编号：3' in output
    assert '被挡数量：3' in output
