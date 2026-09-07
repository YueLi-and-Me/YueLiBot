"""验证插件配置对账：补齐新增字段、报告退休键、逐字段处置非法值。

``ensure_plugin_config`` 曾经只生成不对账：一个模型不认识的残留键让整份配置
校验失败、退回全默认，用户手填的值全部丢失；模型新增的字段也永远不进用户的
文件。这里锁死对账后的行为——用户值不丢、文件只追加不改写、退休键只报告
不删除，处置口径与主配置 ``src.core.config.upgrade`` 文件头一致。

依赖 ``src.plugin_system.config``；全部用例不出网。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib
from pydantic import Field, model_validator
from structlog.testing import capture_logs

from src.plugin_system.config import (
    PluginConfig,
    ensure_plugin_config,
    plugin_config_path,
    render_plugin_config,
)


class _ReconcileConfig(PluginConfig):
    """带两个自定义字段的配置模型，覆盖字符串与整数两种类型。"""

    proxy: str = Field(default='', description='出站代理地址')
    limit: int = Field(default=4000, description='速率上限')


def _write_config(directory: Path, body: str) -> Path:
    """在插件目录里写一份 config.toml，模拟用户已有的文件。"""
    path = plugin_config_path(directory)
    path.write_text(body, encoding='utf-8')
    return path


def test_残留键不再清零其余字段(tmp_path: Path) -> None:
    """一个模型不认识的键曾经让整份配置退回默认；现在只报告该键，其余用户值保留。"""
    directory = tmp_path / 'demo-plugin'
    directory.mkdir()
    path = _write_config(
        directory,
        '[plugin]\n'
        'enabled = true\n'
        'proxy = "http://127.0.0.1:7890"\n'
        'limit = 9000\n'
        'old_key = 1\n',
    )

    with capture_logs() as logs:
        config = ensure_plugin_config(directory, _ReconcileConfig)

    assert config.proxy == 'http://127.0.0.1:7890'
    assert config.limit == 9000
    assert 'old_key = 1' in path.read_text(encoding='utf-8')
    retired = [entry for entry in logs if '没有代码读取' in str(entry.get('event', ''))]
    assert len(retired) == 1
    assert retired[0]['log_level'] == 'warning'
    assert retired[0]['keys'] == ['old_key']
    assert retired[0]['plugin'] == 'demo-plugin'


def test_新增字段补齐且已有内容逐字不动(tmp_path: Path) -> None:
    """模型比文件多出的字段按默认值追加，带 description 注释；已有字节一律不动。"""
    directory = tmp_path / 'demo-plugin'
    directory.mkdir()
    original = (
        '# 顶部是我自己写的说明\n'
        '[plugin]\n'
        '# 这一行注释也要原样留着\n'
        'enabled = true\n'
        'proxy = "http://127.0.0.1:7890"\n'
    )
    path = _write_config(directory, original)

    with capture_logs() as logs:
        config = ensure_plugin_config(directory, _ReconcileConfig)

    assert config.proxy == 'http://127.0.0.1:7890'
    assert config.limit == 4000
    content = path.read_text(encoding='utf-8')
    assert content.startswith(original)
    appended = content[len(original):]
    assert '# 速率上限' in appended
    assert 'limit = 4000' in appended
    added = [entry for entry in logs if '新增配置项' in str(entry.get('event', ''))]
    assert len(added) == 1
    assert added[0]['keys'] == ['limit']
    assert added[0]['plugin'] == 'demo-plugin'


def test_补齐不越过文件中其它表(tmp_path: Path) -> None:
    """追加位置是 [plugin] 段末尾：写到文件尾会落进后面的表，把键补错地方。"""
    directory = tmp_path / 'demo-plugin'
    directory.mkdir()
    path = _write_config(
        directory,
        '[plugin]\nenabled = true\nproxy = "http://127.0.0.1:7890"\n\n[other]\nkeep = 1\n',
    )

    ensure_plugin_config(directory, _ReconcileConfig)

    content = path.read_text(encoding='utf-8')
    assert content.index('limit = 4000') < content.index('[other]')
    parsed = tomllib.loads(content)
    assert parsed['plugin']['limit'] == 4000
    assert parsed['other'] == {'keep': 1}


def test_字段类型错误只退回该字段(tmp_path: Path) -> None:
    """一个字段写错不该牵连其余字段：只有它退回默认，日志说清字段、期望与实际。"""
    directory = tmp_path / 'demo-plugin'
    directory.mkdir()
    _write_config(
        directory,
        '[plugin]\nenabled = true\nproxy = "http://127.0.0.1:7890"\nlimit = "很多"\n',
    )

    with capture_logs() as logs:
        config = ensure_plugin_config(directory, _ReconcileConfig)

    assert config.limit == 4000
    assert config.proxy == 'http://127.0.0.1:7890'
    invalid = [entry for entry in logs if '值非法' in str(entry.get('event', ''))]
    assert len(invalid) == 1
    assert invalid[0]['log_level'] == 'warning'
    assert invalid[0]['field'] == 'limit'
    assert invalid[0]['expected'] == 'int'
    assert invalid[0]['actual'] == "'很多'"
    assert invalid[0]['plugin'] == 'demo-plugin'


def test_模型级校验失败退回全默认(tmp_path: Path) -> None:
    """跨字段校验定位不到单个字段，无法逐字段处置，退回全默认并记 warning。"""

    class _CrossChecked(PluginConfig):
        low: int = Field(default=1, description='下限')
        high: int = Field(default=10, description='上限')

        @model_validator(mode='after')
        def _check_order(self) -> '_CrossChecked':
            if self.low >= self.high:
                raise ValueError('low 必须小于 high')
            return self

    directory = tmp_path / 'demo-plugin'
    directory.mkdir()
    _write_config(directory, '[plugin]\nenabled = true\nlow = 8\nhigh = 2\n')

    with capture_logs() as logs:
        config = ensure_plugin_config(directory, _CrossChecked)

    assert config.low == 1
    assert config.high == 10
    assert any('校验失败' in str(entry.get('event', '')) for entry in logs)


def test_文件不存在时按默认值生成完整配置(tmp_path: Path) -> None:
    """首次发现的行为不变：按模型渲染一份带注释的完整配置，返回默认值。"""
    directory = tmp_path / 'demo-plugin'
    directory.mkdir()

    config = ensure_plugin_config(directory, _ReconcileConfig)

    assert config.proxy == ''
    assert config.limit == 4000
    assert plugin_config_path(directory).read_text(encoding='utf-8') == (
        render_plugin_config(_ReconcileConfig)
    )


@pytest.mark.parametrize('body', [
    'this is not toml [[[',
    '[other]\nenabled = false\n',
    'plugin = 1\n',
])
def test_读不懂的文件按默认值运行(tmp_path: Path, body: str) -> None:
    """非法 TOML、缺 [plugin] 段、[plugin] 不是表：语义不变，按默认值运行并记 warning。"""
    directory = tmp_path / 'demo-plugin'
    directory.mkdir()
    _write_config(directory, body)

    with capture_logs() as logs:
        config = ensure_plugin_config(directory, _ReconcileConfig)

    assert config.proxy == ''
    assert config.limit == 4000
    assert any(entry['log_level'] == 'warning' for entry in logs)


def test_补齐写入失败按内存中的对账结果运行(tmp_path: Path, monkeypatch) -> None:
    """只读安装写不进文件不抛异常：本次仍按用户值加默认补齐的结果运行。"""
    directory = tmp_path / 'demo-plugin'
    directory.mkdir()
    path = _write_config(directory, '[plugin]\nenabled = true\nproxy = "http://127.0.0.1:7890"\n')
    original = path.read_text(encoding='utf-8')

    def _deny(self, *args, **kwargs):
        raise OSError('只读文件系统')

    monkeypatch.setattr(Path, 'write_text', _deny)
    with capture_logs() as logs:
        config = ensure_plugin_config(directory, _ReconcileConfig)

    assert config.proxy == 'http://127.0.0.1:7890'
    assert config.limit == 4000
    assert any('补齐写入失败' in str(entry.get('event', '')) for entry in logs)
    assert path.read_text(encoding='utf-8') == original


def test_补齐是幂等的(tmp_path: Path) -> None:
    """连续对账两次，第二次不再改动文件也不再报告补齐。"""
    directory = tmp_path / 'demo-plugin'
    directory.mkdir()
    path = _write_config(directory, '[plugin]\nenabled = true\nproxy = "http://127.0.0.1:7890"\n')

    ensure_plugin_config(directory, _ReconcileConfig)
    once = path.read_text(encoding='utf-8')
    with capture_logs() as logs:
        ensure_plugin_config(directory, _ReconcileConfig)

    assert path.read_text(encoding='utf-8') == once
    assert not [entry for entry in logs if '新增配置项' in str(entry.get('event', ''))]


def test_嵌套配置模型给出可读错误(tmp_path: Path) -> None:
    """嵌套模型本轮不支持，但错误信息必须说清原因，而不是一句看不懂的类型错误。"""

    class _Nested(PluginConfig):
        depth: int = Field(default=1, description='嵌套层级')

    class _WithNested(PluginConfig):
        child: _Nested = Field(default_factory=_Nested, description='嵌套配置')

    with pytest.raises(TypeError, match='暂不支持嵌套'):
        render_plugin_config(_WithNested)
