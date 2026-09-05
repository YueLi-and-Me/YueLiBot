"""信息框排版的宽度和结构回归测试。"""

from src.core.common.console_layout import display_width, render_box


def test_信息框保留边界并按中文显示宽度对齐() -> None:
    """启动入口框在中文终端中每一行都应拥有同一外框宽度。"""
    rendered = render_box(
        'YueLiBot · WebUI 入口',
        [
            'WebUI 观察面板：http://127.0.0.1:7999',
            ('登录 token', 'a' * 64),
            '状态：后端正在初始化',
        ],
        width=96,
    )

    lines = rendered.splitlines()
    assert lines[0].startswith('╭─ YueLiBot · WebUI 入口')
    assert lines[-1].startswith('╰') and lines[-1].endswith('╯')
    assert len({display_width(line) for line in lines}) == 1
    assert 'a' * 64 in rendered


def test_超长行会换行而不是把终端横向撑开() -> None:
    """长路径或异常文本必须留在信息框内。"""
    rendered = render_box('诊断', ['x' * 240], width=64)

    lines = rendered.splitlines()
    assert len(lines) > 3
    assert max(display_width(line) for line in lines) <= 64
