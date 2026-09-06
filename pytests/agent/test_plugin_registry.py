"""验证插件发现与注册表：目录扫描、类型分派、冲突优先级与故障隔离。

注册表是工具插件进入主体的唯一入口，因此这里锁死的是边界语义：合法插件全部
被发现、坏插件只影响自身且日志带确切原因、同 id 冲突先扫描者生效、聚合调用
不因为单个插件异常而中断。加载器侧验证 ``load_tool_plugin`` 与按基类判定唯一
实现的通用化改造。

依赖 ``src.plugin_system``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import json

import pytest
from structlog.testing import capture_logs

from src.plugin_system import PluginLoadError, load_manifest


def _registry() -> Any:
    """在用例内构造注册表：改动前每个用例各自失败，保留逐条断言的失败证据。"""
    from src.plugin_system import PluginRegistry
    return PluginRegistry()


def _load_tool_plugin(directory: Path, manifest: Any) -> Any:
    """在用例内导入加载函数，理由同 ``_registry``。"""
    from src.plugin_system.loader import load_tool_plugin
    return load_tool_plugin(directory, manifest)


# ------------------------------------------------------------ 插件目录素材

_OBSERVER_PLUGIN = '''
from src.plugin_system import ToolPlugin


class ObserverPlugin(ToolPlugin):
    """记录观察与生命周期调用的工具插件。"""

    def __init__(self, manifest):
        super().__init__(manifest)
        self.seen = []
        self.load_calls = 0
        self.unload_calls = 0

    async def on_load(self):
        self.load_calls += 1

    async def on_unload(self):
        self.unload_calls += 1

    def observe_inbound(self, stream_id, message_id, inbound):
        self.seen.append((stream_id, message_id))

    def stream_capabilities(self, stream_id):
        return frozenset({'forward_message'})
'''

_HISTORY_PLUGIN = '''
from src.plugin_system import ToolPlugin


class HistoryPlugin(ToolPlugin):
    """只贡献另一项能力的工具插件。"""

    def stream_capabilities(self, stream_id):
        return frozenset({'group_history'})
'''

_RAISING_OBSERVER_PLUGIN = '''
from src.plugin_system import ToolPlugin


class RaisingObserverPlugin(ToolPlugin):
    """observe_inbound 恒定抛异常，模拟第三方插件的 bug。"""

    def observe_inbound(self, stream_id, message_id, inbound):
        raise RuntimeError('插件内部错误')
'''

_RAISING_CAPABILITIES_PLUGIN = '''
from src.plugin_system import ToolPlugin


class RaisingCapabilitiesPlugin(ToolPlugin):
    """stream_capabilities 恒定抛异常。"""

    def stream_capabilities(self, stream_id):
        raise RuntimeError('能力查询内部错误')
'''

_FAILING_LOAD_PLUGIN = '''
from src.plugin_system import ToolPlugin


class FailingLoadPlugin(ToolPlugin):
    """on_load 恒定抛异常，模拟配置写错的第三方插件。"""

    async def on_load(self):
        raise RuntimeError('配置缺失')
'''

_MIXED_ENTRY_PLUGIN = '''
from src.plugin_system import AdapterPlugin, ToolPlugin


class ToolSidePlugin(ToolPlugin):
    """与适配器实现同处一个入口模块的工具插件。"""


class AdapterSidePlugin(AdapterPlugin):
    """同一入口里的适配器实现；按基类判定时不得被误判为工具插件候选。"""

    async def probe_capabilities(self):
        return frozenset()

    async def on_start(self):
        pass

    async def on_stop(self):
        pass
'''

_TWO_TOOL_CLASSES = '''
from src.plugin_system import ToolPlugin


class FirstPlugin(ToolPlugin):
    pass


class SecondPlugin(ToolPlugin):
    pass
'''

_NO_TOOL_CLASS = '''
VALUE = 1
'''


def _write_plugin(
    root: Path,
    dirname: str,
    plugin_id: str,
    source: str | None,
    **manifest_overrides: Any,
) -> Path:
    """在临时目录里造一个插件目录；source 为 None 时不写入口文件。"""
    directory = root / dirname
    directory.mkdir(parents=True)
    payload = {
        'manifest_version': 1,
        'id': plugin_id,
        'plugin_type': 'tool',
        'name': dirname,
        'version': '0.0.1',
        'description': '测试用工具插件',
    }
    payload.update(manifest_overrides)
    (directory / '_manifest.json').write_text(
        json.dumps(payload, ensure_ascii=False), encoding='utf-8',
    )
    if source is not None:
        (directory / 'plugin.py').write_text(source, encoding='utf-8')
    return directory


def _write_broken_manifest(root: Path, dirname: str) -> Path:
    """造一个清单缺必填字段的插件目录。"""
    directory = root / dirname
    directory.mkdir(parents=True)
    (directory / '_manifest.json').write_text(
        json.dumps({'manifest_version': 1, 'plugin_type': 'tool'}, ensure_ascii=False),
        encoding='utf-8',
    )
    (directory / 'plugin.py').write_text(_OBSERVER_PLUGIN, encoding='utf-8')
    return directory


def _write_adapter_dir(root: Path, dirname: str, plugin_id: str) -> Path:
    """造一个清单合法但类型为适配器的插件目录。"""
    return _write_plugin(
        root,
        dirname,
        plugin_id,
        None,
        plugin_type='adapter',
        protocol='onebot11',
        config_section=dirname,
        capabilities={'static': ['send_message'], 'probed': []},
    )


def _plugin_by_id(registry: Any, plugin_id: str) -> Any:
    """从注册表里按 id 取出插件实例。"""
    return next(
        plugin for plugin in registry.tool_plugins()
        if plugin.manifest.plugin_id == plugin_id
    )


# ------------------------------------------------------------ ★1 发现


def test_discover_finds_all_valid_tool_plugins(tmp_path: Path) -> None:
    """★1 含两个合法工具插件的目录，两个都被发现并进入注册表。"""
    root = tmp_path / 'plugins'
    _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    _write_plugin(root, 'beta-tool', 'test.beta-tool', _HISTORY_PLUGIN)
    registry = _registry()

    registry.discover([root])

    assert sorted(
        plugin.manifest.plugin_id for plugin in registry.tool_plugins()
    ) == ['test.alpha-tool', 'test.beta-tool']


def test_discover_tolerates_missing_root(tmp_path: Path) -> None:
    """根目录不存在不是错误：用户可能从未放置第三方插件。"""
    registry = _registry()

    registry.discover([tmp_path / 'nope'])

    assert registry.tool_plugins() == ()


# ------------------------------------------------------------ ★2 隔离


def test_invalid_manifest_does_not_block_other_plugins(tmp_path: Path) -> None:
    """★2 混入清单非法的插件时其余插件照常加载，日志含该插件的确切原因。"""
    root = tmp_path / 'plugins'
    _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    broken = _write_broken_manifest(root, 'broken-tool')
    _write_plugin(root, 'beta-tool', 'test.beta-tool', _HISTORY_PLUGIN)
    registry = _registry()

    with capture_logs() as logs:
        registry.discover([root])

    assert sorted(
        plugin.manifest.plugin_id for plugin in registry.tool_plugins()
    ) == ['test.alpha-tool', 'test.beta-tool']
    errors = [entry for entry in logs if entry.get('log_level') == 'error']
    assert any(str(broken) == str(entry.get('directory', '')) for entry in errors), (
        '错误日志必须能定位到出问题的插件目录'
    )
    assert any('清单缺少非空字符串字段' in str(entry.get('error', '')) for entry in errors), (
        '清单非法必须给出确切原因，不能只报「加载失败」'
    )


def test_broken_entry_does_not_block_other_plugins(tmp_path: Path) -> None:
    """入口实现不唯一时只跳过该插件，日志给出歧义原因。"""
    root = tmp_path / 'plugins'
    _write_plugin(root, 'ambiguous-tool', 'test.ambiguous-tool', _TWO_TOOL_CLASSES)
    _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    registry = _registry()

    with capture_logs() as logs:
        registry.discover([root])

    assert [
        plugin.manifest.plugin_id for plugin in registry.tool_plugins()
    ] == ['test.alpha-tool']
    assert any(
        entry.get('log_level') == 'error' and '入口有歧义' in str(entry.get('error', ''))
        for entry in logs
    )


def test_adapter_manifest_in_scanned_root_is_skipped(tmp_path: Path) -> None:
    """适配器互斥且跑在独立进程，扫目录路径发现适配器清单时忽略并记 warning。"""
    root = tmp_path / 'plugins'
    _write_adapter_dir(root, 'stray-adapter', 'test.stray-adapter')
    _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    registry = _registry()

    with capture_logs() as logs:
        registry.discover([root])

    assert [
        plugin.manifest.plugin_id for plugin in registry.tool_plugins()
    ] == ['test.alpha-tool']
    assert any(
        entry.get('log_level') == 'warning'
        and entry.get('plugin') == 'test.stray-adapter'
        for entry in logs
    )


# ------------------------------------------------------------ ★3 入站聚合隔离


def test_raising_observer_does_not_break_inbound_aggregation(tmp_path: Path) -> None:
    """★3 某插件 observe_inbound 抛异常时其余插件仍被调用到，聚合本身不抛。"""
    root = tmp_path / 'plugins'
    _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    _write_plugin(root, 'raising-tool', 'test.raising-tool', _RAISING_OBSERVER_PLUGIN)
    registry = _registry()
    registry.discover([root])

    with capture_logs() as logs:
        registry.observe_inbound(3, 9, object())

    observer = _plugin_by_id(registry, 'test.alpha-tool')
    assert observer.seen == [(3, 9)], '其余插件必须仍被调用到'
    assert any(
        entry.get('log_level') == 'error'
        and entry.get('plugin') == 'test.raising-tool'
        and '插件内部错误' in str(entry.get('error', ''))
        for entry in logs
    )


# ------------------------------------------------------------ ★4 能力并集


def test_stream_capabilities_unions_all_plugins(tmp_path: Path) -> None:
    """★4 stream_capabilities 返回全部插件贡献能力之并。"""
    root = tmp_path / 'plugins'
    _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    _write_plugin(root, 'beta-tool', 'test.beta-tool', _HISTORY_PLUGIN)
    registry = _registry()
    registry.discover([root])

    assert registry.stream_capabilities(3) == frozenset({'forward_message', 'group_history'})


def test_raising_capabilities_does_not_break_union(tmp_path: Path) -> None:
    """能力查询同样在入站/回合主链路上，单插件异常不得中断合并。"""
    root = tmp_path / 'plugins'
    _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    _write_plugin(root, 'raising-tool', 'test.raising-tool', _RAISING_CAPABILITIES_PLUGIN)
    registry = _registry()
    registry.discover([root])

    with capture_logs() as logs:
        capabilities = registry.stream_capabilities(3)

    assert capabilities == frozenset({'forward_message'})
    assert any(entry.get('log_level') == 'error' for entry in logs)


# ------------------------------------------------------------ ★5 冲突优先级


def test_first_scanned_root_wins_on_id_conflict(tmp_path: Path) -> None:
    """★5 内置与第三方目录出现同 id 时，先扫描的内置生效并记 warning。"""
    built_in = tmp_path / 'built_in'
    third_party = tmp_path / 'plugins'
    _write_plugin(built_in, 'shared-tool', 'test.shared-tool', _OBSERVER_PLUGIN, name='内置版')
    _write_plugin(third_party, 'shared-tool', 'test.shared-tool', _HISTORY_PLUGIN, name='第三方版')
    registry = _registry()

    with capture_logs() as logs:
        registry.discover([built_in, third_party])

    plugins = registry.tool_plugins()
    assert [plugin.manifest.plugin_id for plugin in plugins] == ['test.shared-tool']
    assert plugins[0].manifest.name == '内置版', '同 id 冲突时内置（先扫描）必须生效'
    assert any(
        entry.get('log_level') == 'warning'
        and entry.get('plugin') == 'test.shared-tool'
        and '忽略' in str(entry.get('event', ''))
        and str(third_party) in str(entry.get('ignored', ''))
        for entry in logs
    ), 'warning 必须说明第三方那个被忽略了'


# ------------------------------------------------------------ 生命周期


async def test_load_all_drives_on_load_for_every_plugin(tmp_path: Path) -> None:
    """load_all 依次调用每个插件的 on_load。"""
    root = tmp_path / 'plugins'
    _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    _write_plugin(root, 'beta-tool', 'test.beta-tool', _OBSERVER_PLUGIN)
    registry = _registry()
    registry.discover([root])

    await registry.load_all()

    for plugin in registry.tool_plugins():
        assert plugin.load_calls == 1


async def test_load_all_isolates_failing_plugin(tmp_path: Path) -> None:
    """单个插件 on_load 失败不得拖垮其余插件：记 error、移除该插件、继续。"""
    root = tmp_path / 'plugins'
    _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    _write_plugin(root, 'failing-tool', 'test.failing-tool', _FAILING_LOAD_PLUGIN)
    registry = _registry()
    registry.discover([root])

    with capture_logs() as logs:
        await registry.load_all()

    assert [
        plugin.manifest.plugin_id for plugin in registry.tool_plugins()
    ] == ['test.alpha-tool']
    assert _plugin_by_id(registry, 'test.alpha-tool').load_calls == 1
    assert any(
        entry.get('log_level') == 'error'
        and entry.get('plugin') == 'test.failing-tool'
        and '配置缺失' in str(entry.get('error', ''))
        for entry in logs
    )


async def test_unload_all_is_idempotent(tmp_path: Path) -> None:
    """unload_all 幂等：停机与重载两条路径都会调用，重复调用不得重复卸载。"""
    root = tmp_path / 'plugins'
    _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    registry = _registry()
    registry.discover([root])
    await registry.load_all()

    await registry.unload_all()
    await registry.unload_all()

    assert _plugin_by_id(registry, 'test.alpha-tool').unload_calls == 1


async def test_unload_all_without_load_is_noop(tmp_path: Path) -> None:
    """尚未 load_all 时 unload_all 是空操作，不调用任何插件的 on_unload。"""
    root = tmp_path / 'plugins'
    _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    registry = _registry()
    registry.discover([root])

    await registry.unload_all()

    assert _plugin_by_id(registry, 'test.alpha-tool').unload_calls == 0


# ------------------------------------------------------------ 加载器通用化


def test_load_tool_plugin_rejects_adapter_manifest(tmp_path: Path) -> None:
    """工具加载路径收到适配器清单时当场拒绝，而不是带进运行期。"""
    directory = _write_adapter_dir(tmp_path, 'stray-adapter', 'test.stray-adapter')
    manifest = load_manifest(directory / '_manifest.json')

    with pytest.raises(PluginLoadError, match='不是工具插件'):
        _load_tool_plugin(directory, manifest)


def test_load_tool_plugin_requires_unique_tool_plugin_class(tmp_path: Path) -> None:
    """入口没有或有多余工具插件实现时给出确切原因，与适配器同一套判定逻辑。"""
    empty = _write_plugin(tmp_path, 'empty-tool', 'test.empty-tool', _NO_TOOL_CLASS)
    ambiguous = _write_plugin(
        tmp_path, 'ambiguous-tool', 'test.ambiguous-tool', _TWO_TOOL_CLASSES,
    )

    with pytest.raises(PluginLoadError, match='没有定义 ToolPlugin 子类'):
        _load_tool_plugin(empty, load_manifest(empty / '_manifest.json'))
    with pytest.raises(PluginLoadError, match='入口有歧义'):
        _load_tool_plugin(ambiguous, load_manifest(ambiguous / '_manifest.json'))


def test_load_tool_plugin_ignores_adapter_classes_in_entry(tmp_path: Path) -> None:
    """唯一实现判定按期望基类过滤：入口里的适配器类不计入工具插件候选。"""
    directory = _write_plugin(tmp_path, 'mixed-tool', 'test.mixed-tool', _MIXED_ENTRY_PLUGIN)
    manifest = load_manifest(directory / '_manifest.json')

    plugin = _load_tool_plugin(directory, manifest)

    assert type(plugin).__name__ == 'ToolSidePlugin'
    assert plugin.manifest.plugin_id == 'test.mixed-tool'


# ------------------------------------------------------------ 插件自带开关

def _write_switch(directory: Path, body: str) -> None:
    """在插件目录里写一份 config.toml。"""
    (directory / 'config.toml').write_text(body, encoding='utf-8')


def test_关闭的插件不进注册表而同目录其余插件照常加载(tmp_path: Path) -> None:
    """开关是每个插件自己的事，关掉一个不影响另一个。"""
    root = tmp_path / 'plugins'
    alpha = _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    _write_plugin(root, 'beta-tool', 'test.beta-tool', _HISTORY_PLUGIN)
    _write_switch(alpha, '[plugin]\nenabled = false\n')
    registry = _registry()

    registry.discover([root])

    assert [
        plugin.manifest.plugin_id for plugin in registry.tool_plugins()
    ] == ['test.beta-tool']


def test_关闭的插件入口模块不会被执行(tmp_path: Path) -> None:
    """跳过发生在加载之前：入口写坏的插件也能靠开关绕开，不必删目录。"""
    root = tmp_path / 'plugins'
    broken = _write_plugin(root, 'broken-tool', 'test.broken-tool', 'this is not valid python (')
    _write_switch(broken, '[plugin]\nenabled = false\n')
    registry = _registry()

    with capture_logs() as logs:
        registry.discover([root])

    assert registry.tool_plugins() == ()
    assert not [entry for entry in logs if entry['log_level'] == 'error'], logs
    assert any('关闭' in str(entry.get('event', '')) for entry in logs), logs


def test_没有配置文件的插件视为启用(tmp_path: Path) -> None:
    """向后兼容：先于本机制存在的插件都没有 config.toml，不能因此失能。"""
    root = tmp_path / 'plugins'
    _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    registry = _registry()

    registry.discover([root])

    assert len(registry.tool_plugins()) == 1


def test_显式写_true_的插件加载(tmp_path: Path) -> None:
    """开关的正向路径。"""
    root = tmp_path / 'plugins'
    alpha = _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    _write_switch(alpha, '[plugin]\nenabled = true\n')
    registry = _registry()

    registry.discover([root])

    assert len(registry.tool_plugins()) == 1


@pytest.mark.parametrize('body', [
    'this is not toml [[[',
    '[plugin]\nenabled = "no"\n',
    '[other]\nenabled = false\n',
    '[plugin]\nname = "x"\n',
])
def test_坏配置按启用处理并记警告(tmp_path: Path, body: str) -> None:
    """读不懂不等于要关掉——把笔误当成关闭意图，等于让它悄悄拿掉一项能力。"""
    root = tmp_path / 'plugins'
    alpha = _write_plugin(root, 'alpha-tool', 'test.alpha-tool', _OBSERVER_PLUGIN)
    _write_switch(alpha, body)
    registry = _registry()

    registry.discover([root])

    assert len(registry.tool_plugins()) == 1
