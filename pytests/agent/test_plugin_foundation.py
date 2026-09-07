"""锁定工具插件的公开导入边界、包入口与宿主契约，全部使用本地桩件。"""

from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from structlog.testing import capture_logs
from typing import Any, Dict, Iterator, List

import pytest
import re
import sys

from src.core.app_meta import APP_VERSION
from src.core.config.schema import Config
from src.core.logging.logger_colors import module_alias, module_color
from src.plugin_system import (
    SUPPORTED_MANIFEST_VERSION,
    HostView,
    ManifestError,
    Plugin,
    PluginConfig,
    PluginContext,
    PluginManifest,
    PluginPaths,
    load_adapter_plugin,
    load_manifest,
    load_tool_plugin,
    manifest_from_payload,
)


ROOT = Path(__file__).resolve().parents[2]


def _boundary_violations(root: Path) -> List[str]:
    """直接扫描入口源码，返回越界 import 的相对文件名、行号和原文。"""
    violations: List[str] = []
    forbidden = re.compile(r'\b(?:from\s+src\.(?:core|platforms)\b|import\s+src\.(?:core|platforms)\b)')
    for pattern in ('src/plugins/built_in/*/plugin.py', 'plugins/*/plugin.py'):
        for path in sorted(root.glob(pattern)):
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if forbidden.search(line):
                    violations.append(f'{path.relative_to(root).as_posix()}:{number}: {line.strip()}')
    return violations


def test_tool_plugin_import_boundary() -> None:
    """工具插件只能通过公开契约取得内核类型；违规直接定位到源码行。"""
    violations = _boundary_violations(ROOT)
    assert not violations, '插件越过公开 API 边界：\n' + '\n'.join(violations)


def _manifest(plugin_id: str) -> PluginManifest:
    return PluginManifest(plugin_id, 'tool', '测试插件', '0.1.0', '本地加载验收')


@pytest.fixture
def module_namespace() -> Iterator[str]:
    """测试结束摘除专用模块名，避免同进程测试之间共享插件缓存。"""
    name = 'test_foundation-two-files'
    yield name
    for key in list(sys.modules):
        if key == name or key.startswith(name + '.'):
            del sys.modules[key]


def test_two_file_tool_plugin(tmp_path: Path, module_namespace: str) -> None:
    """带连字符的包入口能从兄弟模块导入，且 dataclass 能查到模块身份。"""
    (tmp_path / 'helper.py').write_text('VALUE = 37\n', encoding='utf-8')
    (tmp_path / 'plugin.py').write_text(
        'from __future__ import annotations\n'
        'from dataclasses import dataclass\n'
        'from .helper import VALUE\n'
        'from src.plugin_system import ToolPlugin\n'
        '@dataclass\nclass Record:\n    value: int = VALUE\n'
        'class Sample(ToolPlugin):\n    value = Record().value\n',
        encoding='utf-8',
    )
    plugin = load_tool_plugin(tmp_path, _manifest(module_namespace))
    assert plugin.value == 37
    assert sys.modules[module_namespace].Sample is type(plugin)


@pytest.mark.parametrize('root', ['src/plugins/built_in', 'plugins'])
@pytest.mark.parametrize('statement', [
    'from src.core.db import connection',
    'import src.core.db',
    'from src.platforms.onebot11 import host',
    'import src.platforms.onebot11',
])
def test_boundary_reports_file_and_line(tmp_path: Path, root: str, statement: str) -> None:
    """两类工具插件目录中四种越界 import 都必须给出准确行号。"""
    path = tmp_path / root / 'sample' / 'plugin.py'
    path.parent.mkdir(parents=True)
    path.write_text('# 测试入口\n' + statement + '\n', encoding='utf-8')
    assert _boundary_violations(tmp_path) == [f'{root}/sample/plugin.py:2: {statement}']


def test_failed_package_is_removed_and_retry_reexecutes(tmp_path: Path, module_namespace: str) -> None:
    """入口失败不遗留已导入兄弟模块，重试必须重新执行两份源码。"""
    marker = tmp_path / 'calls.txt'
    (tmp_path / 'helper.py').write_text(
        'from pathlib import Path\n'
        'marker = Path(__file__).with_name("calls.txt")\n'
        'with marker.open("a", encoding="utf-8") as output:\n    output.write("helper\\n")\n',
        encoding='utf-8',
    )
    entry = tmp_path / 'plugin.py'
    entry.write_text(
        'from .helper import marker\n'
        'from src.plugin_system import ToolPlugin\n'
        'with marker.open("a", encoding="utf-8") as output:\n    output.write("entry\\n")\n'
        'if not marker.with_name("ready").exists():\n    raise RuntimeError("入口故障")\n'
        'class Sample(ToolPlugin):\n    pass\n', encoding='utf-8',
    )
    with pytest.raises(RuntimeError, match='入口故障'):
        load_tool_plugin(tmp_path, _manifest(module_namespace))
    assert not any(key == module_namespace or key.startswith(module_namespace + '.') for key in sys.modules)
    (tmp_path / 'ready').touch()
    load_tool_plugin(tmp_path, _manifest(module_namespace))
    assert marker.read_text(encoding='utf-8').splitlines() == ['helper', 'entry', 'helper', 'entry']


def _payload(**overrides: Any) -> Dict[str, Any]:
    payload = {
        'manifest_version': SUPPORTED_MANIFEST_VERSION,
        'id': 'test.foundation-two-files',
        'plugin_type': 'tool',
        'name': '测试插件',
        'version': '0.1.0',
        'description': '本地契约验收',
    }
    payload.update(overrides)
    return payload


def test_two_file_adapter_uses_same_loader(tmp_path: Path, module_namespace: str) -> None:
    """独立进程适配器共享包加载能力，不需另一份加载实现。"""
    import json

    (tmp_path / '_manifest.json').write_text(json.dumps(_payload(
        plugin_type='adapter', protocol='onebot11', config_section='sample',
        capabilities={'static': [], 'probed': []},
    )), encoding='utf-8')
    (tmp_path / 'helper.py').write_text('VALUE = 37\n', encoding='utf-8')
    (tmp_path / 'plugin.py').write_text(
        'from .helper import VALUE\n'
        'from src.plugin_system import AdapterPlugin\n'
        'class Sample(AdapterPlugin):\n'
        '    value = VALUE\n'
        '    async def probe_capabilities(self): return frozenset()\n'
        '    async def on_start(self): pass\n'
        '    async def on_stop(self): pass\n', encoding='utf-8',
    )
    assert load_adapter_plugin(tmp_path).value == 37


def test_context_requires_injection_and_exposes_only_three_views(tmp_path: Path) -> None:
    """提前访问当场报错；注入只暴露三样，并复制宿主值而非完整配置对象。"""
    plugin = Plugin(_manifest('test.context'))
    with pytest.raises(AttributeError, match=r'test.context.*尚未注入.*bind_config.*on_load.*bind_context'):
        _ = plugin.ctx
    cfg = Config()
    cfg.bot.name = '测试宿主'
    cfg.advanced.https_proxy = 'http://127.0.0.1:9999'
    data_dir = tmp_path / 'data'
    ctx = PluginContext('test.context', tmp_path, data_dir, cfg)
    plugin.bind_config(PluginConfig())
    plugin.bind_context(ctx)
    assert plugin.ctx is ctx
    assert {name for name in dir(ctx) if not name.startswith('_')} == {'logger', 'paths', 'host'}
    assert ctx.paths == PluginPaths(tmp_path, data_dir)
    assert ctx.host == HostView(cfg.bot.name, cfg.advanced.https_proxy, data_dir)
    assert ctx.host.data_dir is ctx.paths.data_dir
    assert [field.name for field in fields(HostView)] == ['bot_name', 'https_proxy', 'data_dir']
    assert [field.name for field in fields(PluginPaths)] == ['plugin_dir', 'data_dir']
    assert not data_dir.exists()
    with pytest.raises(FrozenInstanceError):
        ctx.host.bot_name = '不可修改'
    with pytest.raises(FrozenInstanceError):
        ctx.paths.plugin_dir = data_dir
    with pytest.raises(AttributeError):
        ctx.host = HostView('', '', data_dir)
    cfg.bot.name = '后来修改'
    assert ctx.host.bot_name == '测试宿主'
    with capture_logs() as logs:
        ctx.logger.info('上下文日志验收')
    assert logs[0]['logger'] == 'src.plugin_system.context.test.context'
    assert module_alias('plugin_system.context.test.context') == '插件·test.context'
    assert module_color('plugin_system.context.test.context')


@pytest.mark.asyncio
async def test_context_is_available_in_on_load(tmp_path: Path) -> None:
    """按宿主约定顺序注入后，插件 on_load 可立即消费上下文。"""
    class Sample(Plugin):
        async def on_load(self):
            assert self.ctx.paths.plugin_dir == tmp_path
            assert self.config.enabled

    plugin = Sample(_manifest('test.context-lifecycle'))
    plugin.bind_config(PluginConfig())
    plugin.bind_context(PluginContext(plugin.manifest.plugin_id, tmp_path, tmp_path, Config()))
    await plugin.on_load()


def test_all_shipped_manifests_use_supported_version() -> None:
    """四份随程序分发的清单必须共同升级，适配器不能被遗漏。"""
    paths = [*ROOT.glob('adapters/*/_manifest.json'), *ROOT.glob('src/plugins/built_in/*/_manifest.json')]
    assert len(paths) == 4
    for path in paths:
        assert load_manifest(path).plugin_id


def test_manifest_metadata_is_preserved_and_immutable() -> None:
    """兼容性及发布元数据在清单中可读，调用方修改原始 JSON 不会污染模型。"""
    payload = _payload(
        host_application={'min_version': APP_VERSION, 'max_version': APP_VERSION},
        author={'name': '测试作者', 'url': 'https://example.invalid/author'},
        license='MIT', urls={'repository': 'https://example.invalid/repository'},
    )
    manifest = manifest_from_payload(payload)
    assert manifest.host_application == payload['host_application']
    assert manifest.author == payload['author']
    assert manifest.license == 'MIT'
    assert manifest.urls == payload['urls']
    payload['author']['name'] = '篡改'
    assert manifest.author['name'] == '测试作者'
    for mapping in (manifest.author, manifest.host_application, manifest.urls):
        with pytest.raises(TypeError):
            mapping['new'] = '不允许'
    assert manifest_from_payload(_payload(author='作者')).author == '作者'
    assert manifest_from_payload(_payload()).author is None


@pytest.mark.parametrize('host_version,bounds,accepted', [
    ('1.2.9', {'min_version': '1.2.10'}, False),
    ('1.2.10', {'min_version': '1.2.9'}, True),
    ('1.2.10', {'max_version': '1.2.9'}, False),
    ('1.2.9', {'max_version': '1.2.10'}, True),
    ('1.2.9', {'min_version': '1.2.9', 'max_version': '1.2.9'}, True),
])
def test_host_version_interval(monkeypatch, host_version, bounds, accepted) -> None:
    """闭区间按整数比较，不能把 10 排到 9 前面。"""
    monkeypatch.setattr('src.plugin_system.manifest.APP_VERSION', host_version)
    if accepted:
        manifest_from_payload(_payload(host_application=bounds))
    else:
        with pytest.raises(ManifestError, match='宿主版本不匹配'):
            manifest_from_payload(_payload(host_application=bounds))


def test_too_new_manifest_rejected_before_entry_executes(tmp_path: Path) -> None:
    """真实宿主版本不足时，适配器加载入口也必须止步于清单校验。"""
    import json

    major, minor, patch = map(int, APP_VERSION.split('.'))
    required = f'{major + 1}.{minor}.{patch}'
    (tmp_path / '_manifest.json').write_text(json.dumps(_payload(
        host_application={'min_version': required}, plugin_type='adapter',
        protocol='onebot11', config_section='sample', capabilities={},
    )), encoding='utf-8')
    (tmp_path / 'plugin.py').write_text('raise AssertionError("不能执行入口")\n', encoding='utf-8')
    with pytest.raises(ManifestError, match='宿主版本不匹配'):
        load_adapter_plugin(tmp_path)


@pytest.mark.parametrize('overrides,reason', [
    ({'host_application': []}, 'host_application'),
    ({'host_application': {'min_version': 123}}, 'host_application.min_version'),
    ({'host_application': {'min_version': '1.2.3rc1'}}, 'x.y.z'),
    ({'host_application': {'max_version': '1.2.3+build'}}, 'x.y.z'),
    ({'host_application': {'min_version': 'v1.2.3'}}, 'x.y.z'),
    ({'host_application': {'min_version': '1.2'}}, 'x.y.z'),
    ({'host_application': {'min_version': '01.2.3'}}, 'x.y.z'),
    ({'host_application': {'min_version': '2.0.0', 'max_version': '1.0.0'}}, '版本区间非法'),
    ({'author': 3}, 'author'),
    ({'author': {}}, 'author.name'),
    ({'author': {'name': '作者', 'url': False}}, 'author.url'),
    ({'license': []}, 'license'),
    ({'urls': 'bad'}, 'urls'),
    ({'urls': {'repository': []}}, 'urls.repository'),
])
def test_invalid_metadata_rejected(overrides, reason) -> None:
    with pytest.raises(ManifestError, match=re.escape(reason)):
        manifest_from_payload(_payload(**overrides))


@pytest.mark.parametrize('version', [SUPPORTED_MANIFEST_VERSION - 1, SUPPORTED_MANIFEST_VERSION + 1, str(SUPPORTED_MANIFEST_VERSION), float(SUPPORTED_MANIFEST_VERSION), True])
def test_manifest_version_requires_exact_integer(version) -> None:
    """拒绝旧版、未来版及看似相等的浮点数，不引入双版本语义。"""
    with pytest.raises(ManifestError, match='清单格式版本'):
        manifest_from_payload(_payload(manifest_version=version))
