"""入库的配置模板必须与 schema 渲染出的结果逐字一致。

config.example/ 是随代码分发的配置模板，让人在 clone 之前就能看清要准备什么。
它由 src.core.config.bootstrap.render_example_configs 渲染，与首次运行创建真实
配置走同一套 schema 和同一个带注释 TOML 写入器。

这条用例防的是「字段改了、模板忘了重新生成」：手写模板烂掉是不会有人发现的，
而模板一旦与代码不符，照着它准备的人会在启动时撞上一堆本可避免的报错。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config.bootstrap import render_example_configs

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = REPO_ROOT / 'config.example'


def _normalized(path: Path) -> str:
    """读取文本并统一换行，避免 CRLF/LF 差异造成假红。

    仓库在 Windows 上开发，工作区换行由 core.autocrlf 决定；渲染出的临时文件与
    入库副本可能一个 CRLF 一个 LF，那不是内容差异。
    """
    return path.read_text(encoding='utf-8').replace('\r\n', '\n')


def test_committed_example_matches_freshly_rendered(tmp_path: Path) -> None:
    """重新渲染一份，与入库副本比对文件集合与逐字内容。"""
    rendered = render_example_configs(tmp_path)

    expected = {p.relative_to(tmp_path).as_posix() for p in rendered}
    # README.md 是手写的用法说明，不由渲染器产出，比对时排除
    committed = {
        p.relative_to(EXAMPLE_DIR).as_posix()
        for p in EXAMPLE_DIR.rglob('*')
        if p.is_file() and p.name != 'README.md'
    }
    assert committed == expected, (
        '入库模板的文件集合与渲染结果不一致；'
        '重新生成：uv run python -c "from pathlib import Path; '
        'from src.core.config.bootstrap import render_example_configs; '
        "render_example_configs(Path('config.example'))\""
    )

    for relative in sorted(expected):
        assert _normalized(EXAMPLE_DIR / relative) == _normalized(tmp_path / relative), (
            f'config.example/{relative} 与 schema 渲染结果不一致，需要重新生成'
        )


def test_example_carries_no_real_values() -> None:
    """模板里不得出现真实密钥、QQ 号或本机路径。"""
    # 反斜杠用 chr(92) 拼，避免这条断言自己被转义规则绊倒
    drive_prefix = ':' + chr(92)
    forbidden = ('787283783', 'sk-', drive_prefix)
    for path in EXAMPLE_DIR.rglob('*.toml'):
        text = path.read_text(encoding='utf-8')
        for token in forbidden:
            assert token not in text, f'{path.name} 含疑似真实值：{token}'


@pytest.mark.parametrize('name', ['bot.toml', 'features.toml', 'models.toml', 'providers.toml'])
def test_every_field_carries_a_comment(name: str) -> None:
    """四份主配置的每个赋值行都要带行内注释。

    模板的全部价值就在注释：没有注释的字段等于让人去猜取值范围。
    """
    text = (EXAMPLE_DIR / name).read_text(encoding='utf-8')
    bare = [
        line for line in text.splitlines()
        if '=' in line
        and not line.lstrip().startswith('#')
        and '#' not in line.split('=', 1)[1]
    ]
    # [inner] version 是结构版本号，不面向用户，允许无注释
    bare = [line for line in bare if not line.startswith('version =')]
    assert not bare, f'{name} 有字段没有行内说明：{bare}'
