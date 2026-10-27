"""验证插件依赖声明、隔离检查和受约束安装的宿主契约。

依赖进程与安装元数据均使用桩件；临时插件由注册表实际发现，不访问包源。
"""

from importlib import import_module
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from structlog.testing import capture_logs
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import Mock

import json
import subprocess

import pytest

from src.plugin_system.manifest import ManifestError, load_manifest, manifest_from_payload
from src.plugin_system.registry import PluginRegistry


def payload(**changes: Any) -> Dict[str, Any]:
    """返回支持当前宿主的工具清单，changes 覆盖单项以验证错误边界。"""
    return dict(manifest_version=2, id='test.dependencies', plugin_type='tool',
                name='依赖检查', version='1.0.0', description='测试依赖检查',
                host_application={'min_version': '0.2.0', 'max_version': '0.2.5'}, **changes)


def dependency_module():
    """在用例执行时导入新模块，使实现前每条用例保留独立失败记录。"""
    return import_module('src.plugin_system.dependencies')


def plugin(root: Path, name: str, requirements: List[str]) -> Path:
    """在临时根目录生成可真实加载的最小工具插件，返回其目录。"""
    directory = root / name
    directory.mkdir(parents=True)
    raw = payload(python_dependencies=requirements)
    raw['id'] = name
    (directory / '_manifest.json').write_text(json.dumps(raw), encoding='utf-8')
    (directory / 'plugin.py').write_text(
        'from src.plugin_system import ToolPlugin\nclass Example(ToolPlugin):\n    pass\n', encoding='utf-8')
    return directory


def test_manifest_dependencies_default_and_valid():
    assert manifest_from_payload(payload()).python_dependencies == ()
    values = ['respx>=0.23', 'some-package; sys_platform == "win32"']
    assert manifest_from_payload(payload(python_dependencies=values)).python_dependencies == tuple(values)


@pytest.mark.parametrize('value,original', [
    ('respx', 'respx'), ([''], "''"), ([4], '4'), (['bad??'], 'bad??'),
    (['foo @ https://example.org/foo.whl'], 'foo @ https://example.org/foo.whl'),
    (['foo @ file:///tmp/foo'], 'foo @ file:///tmp/foo'),
    (['Foo_Bar', 'foo-bar'], 'foo-bar'),
])
def test_manifest_dependencies_reject_invalid(value, original):
    with pytest.raises(ManifestError) as error:
        manifest_from_payload(payload(python_dependencies=value))
    assert original in str(error.value)
    assert 'python_dependencies' in str(error.value)
    if isinstance(value, list) and value and isinstance(value[0], str) and ' @ ' in value[0]:
        assert '只接受索引上发布的包' in str(error.value)


def test_manifest_version_and_existing_plugins():
    root = Path(__file__).resolve().parents[2]
    paths = [*root.glob('src/plugins/built_in/*/_manifest.json'), *root.glob('adapters/*/_manifest.json')]
    assert len(paths) == 5
    for path in paths:
        raw = json.loads(path.read_text(encoding='utf-8'))
        manifest = load_manifest(path)
        assert manifest.python_dependencies == ()
        assert (manifest.plugin_id, manifest.name, manifest.version) == (raw['id'], raw['name'], raw['version'])
    for value in (1, 3, '2', True):
        raw = payload()
        raw['manifest_version'] = value
        with pytest.raises(ManifestError):
            manifest_from_payload(raw)


def test_unsatisfied_versions_and_markers(monkeypatch):
    module = dependency_module()
    versions = {'present': '2.0', 'old': '1.0', 'preview': '2.0rc1'}
    def version(name):
        if name not in versions:
            raise PackageNotFoundError(name)
        return versions[name]
    monkeypatch.setattr(module.metadata, 'version', version)
    result = module.unsatisfied(['present>=2', 'absent>=1', 'old>=2', 'skip; python_version < "1"', 'preview>=2.0rc1'])
    assert [(item.requirement, item.installed) for item in result] == [('absent>=1', None), ('old>=2', '1.0')]


def test_constraints_filter_and_failure(tmp_path, monkeypatch):
    module = dependency_module()
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='yueli @ file:///D:/project\n-e local\n\nhttpx==0.28.1\nFoo_Bar==2.0rc1\n', stderr=''))
    monkeypatch.setattr(module.subprocess, 'run', run)
    destination = tmp_path / 'constraints.txt'
    assert module.write_constraints('python.exe', 'uv.exe', destination) == destination
    assert destination.read_text(encoding='utf-8') == 'httpx==0.28.1\nFoo_Bar==2.0rc1\n'
    assert run.call_args.args[0] == ['uv.exe', 'pip', 'freeze', '--python', 'python.exe']
    run.return_value = SimpleNamespace(returncode=1, stdout='', stderr='freeze failed 原文')
    with pytest.raises(RuntimeError, match='freeze failed 原文'):
        module.write_constraints('python.exe', 'uv.exe', destination)


def test_install_command_proxy_timeout_and_summary(tmp_path, monkeypatch):
    module = dependency_module()
    run = Mock(return_value=SimpleNamespace(returncode=1, stdout='', stderr='x' * 2200 + '末尾'))
    monkeypatch.setattr(module.subprocess, 'run', run)
    constraints = tmp_path / 'constraints.txt'
    result = module.install(['respx>=0.23'], uv='uv.exe', python='python.exe', constraints=constraints, proxy='http://localhost:7890')
    assert run.call_args.args[0] == ['uv.exe', 'pip', 'install', '--python', 'python.exe', '--constraint', str(constraints), 'respx>=0.23']
    assert run.call_args.kwargs['timeout'] == 300
    assert run.call_args.kwargs['env']['HTTPS_PROXY'] == 'http://localhost:7890'
    assert run.call_args.kwargs['env']['HTTP_PROXY'] == 'http://localhost:7890'
    assert not result.success and result.returncode == 1
    assert len(result.stderr) <= 2000 and result.stderr.endswith('末尾')
    run.side_effect = subprocess.TimeoutExpired('uv', 300, stderr=b'timed out')
    assert not module.install(['respx'], uv='uv', python='python', constraints=constraints, proxy='').success


def test_find_uv_and_missing_executable(tmp_path, monkeypatch):
    module = dependency_module()
    monkeypatch.setenv('UV', 'preferred-uv')
    monkeypatch.setattr(module.shutil, 'which', lambda _: 'path-uv')
    assert module.find_uv() == 'preferred-uv'
    monkeypatch.delenv('UV')
    assert module.find_uv() == 'path-uv'
    monkeypatch.setattr(module.shutil, 'which', lambda _: None)
    assert module.find_uv() is None
    directory = plugin(tmp_path / 'plugins', 'missing', ['absent>=1'])
    monkeypatch.setattr(module, 'unsatisfied', lambda _: [module.DependencyProblem('absent>=1', None)])
    run = Mock(side_effect=AssertionError('禁止子进程'))
    monkeypatch.setattr(module.subprocess, 'run', run)
    notes = module.ensure_plugin_dependencies([(directory.parent, True)], auto_install=True, data_dir=tmp_path, proxy='')
    assert 'uv pip install --python "' in notes[directory]
    assert (tmp_path / 'plugin-deps/constraints.txt').is_file()
    run.assert_not_called()


def test_disabled_satisfied_and_skipped_plugins(tmp_path, monkeypatch):
    module = dependency_module()
    root = tmp_path / 'plugins'
    missing = plugin(root, 'missing', ['absent>=1'])
    plugin(root, '.staging', ['absent>=1'])
    stopped = plugin(root, 'stopped', ['absent>=1'])
    (stopped / 'config.toml').write_text('[plugin]\nenabled=false\n', encoding='utf-8')
    builtin = plugin(tmp_path / 'builtin', 'builtin', ['absent>=1'])
    monkeypatch.setattr(module, 'unsatisfied', lambda _: [module.DependencyProblem('absent>=1', None)])
    run = Mock(side_effect=AssertionError('禁止子进程'))
    monkeypatch.setattr(module.subprocess, 'run', run)
    notes = module.ensure_plugin_dependencies([(root, True), (builtin.parent, False)], auto_install=False, data_dir=tmp_path, proxy='')
    assert set(notes) == {missing}
    assert '自动安装已关闭' in notes[missing] and '"absent>=1"' in notes[missing]
    assert (tmp_path / 'plugin-deps/constraints.txt').is_file()
    monkeypatch.setattr(module, 'unsatisfied', lambda _: [])
    assert module.ensure_plugin_dependencies([(root, True)], auto_install=True, data_dir=tmp_path, proxy='') == {}
    run.assert_not_called()


def test_install_refreshes_constraints_between_plugins(tmp_path, monkeypatch):
    module = dependency_module()
    root = tmp_path / 'plugins'
    plugin(root, 'b', ['second>=1'])
    plugin(root, 'a', ['first>=1'])
    installed = set()
    calls = []
    monkeypatch.setattr(module, 'find_uv', lambda: 'uv')
    monkeypatch.setattr(module, 'unsatisfied', lambda reqs: [module.DependencyProblem(r, None) for r in reqs if r not in installed])
    def constraints(python, uv, path):
        calls.append(('freeze', tuple(sorted(installed))))
        return path
    def install(reqs, **kwargs):
        calls.append(('install', tuple(reqs)))
        installed.update(reqs)
        return module.InstallResult(True, 0, '')
    monkeypatch.setattr(module, 'write_constraints', constraints)
    monkeypatch.setattr(module, 'install', install)
    assert module.ensure_plugin_dependencies([(root, True)], auto_install=True, data_dir=tmp_path, proxy='') == {}
    second = calls.index(('install', ('second>=1',)))
    assert ('freeze', ('first>=1',)) in calls[1:second]
    assert calls[1] == ('install', ('first>=1',))


def test_install_failure_retains_reason_and_manual_command(tmp_path, monkeypatch):
    module = dependency_module()
    directory = plugin(tmp_path / 'plugins', 'conflict', ['httpx<0.28'])
    monkeypatch.setattr(module, 'find_uv', lambda: 'uv')
    monkeypatch.setattr(module, 'unsatisfied', lambda _: [module.DependencyProblem('httpx<0.28', '0.28.1')])
    monkeypatch.setattr(module, 'write_constraints', lambda python, uv, path: path)
    monkeypatch.setattr(module, 'install', lambda *a, **k: module.InstallResult(False, 1, 'httpx<0.28 and httpx==0.28.1 unsatisfiable'))
    notes = module.ensure_plugin_dependencies([(directory.parent, True)], auto_install=True, data_dir=tmp_path, proxy='')
    assert 'httpx<0.28 and httpx==0.28.1 unsatisfiable' in notes[directory]
    assert '可能导致月璃启动失败' in notes[directory]
    assert 'uv pip install --python "' in notes[directory]
    assert '0.28.1' in notes[directory]


def test_registry_dependency_status_and_hidden_directory(tmp_path, monkeypatch):
    module = dependency_module()
    missing = plugin(tmp_path, 'missing', ['absent>=1'])
    plugin(tmp_path, 'working', [])
    plugin(tmp_path, '.staging', [])
    monkeypatch.setattr(module.metadata, 'version', Mock(side_effect=PackageNotFoundError('absent')))
    registry = PluginRegistry(dependency_notes={missing: '缺依赖；手动命令原文'})
    registry.discover([(tmp_path, True)])
    states = {item.plugin_id: item for item in registry.statuses()}
    assert set(states) == {'missing', 'working'}
    assert states['missing'].decision == '依赖缺失'
    assert states['missing'].reason == '缺依赖；手动命令原文'
    assert [item.manifest.plugin_id for item in registry.tool_plugins()] == ['working']


def test_market_config_generation_reconciliation_and_example(tmp_path):
    module = import_module('src.core.plugin_market.config')
    config = module.load_plugin_market_config(tmp_path)
    assert config.dependencies.auto_install is True
    assert config.sources[0].index_url == 'https://plugins.yuelibot.org/index.json'
    path = tmp_path / 'plugin_market.toml'
    assert path.read_text(encoding='utf-8') == module.plugin_market_template()
    example = Path(__file__).resolve().parents[2] / 'config.example/plugin_market.toml'
    assert example.read_text(encoding='utf-8') == module.plugin_market_template()
    original = '# 保留我的注释\nsources = []\nlegacy = 1\n[dependencies]\n# 保留开关位置\n'
    path.write_text(original, encoding='utf-8')
    with capture_logs() as logs:
        config = module.load_plugin_market_config(tmp_path)
    assert config.sources == [] and config.dependencies.auto_install
    assert original in path.read_text(encoding='utf-8')
    assert any('legacy' in str(log) for log in logs)
    before = path.read_bytes()
    module.load_plugin_market_config(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('url,valid', [('http://example.org/index.json', False), ('https://example.org/index.json', True), ('http://127.0.0.1:8000/index.json', True), ('http://[::1]/index.json', True), ('http://localhost/index.json', True), ('file:///index.json', False)])
def test_market_config_url_validation(tmp_path, url, valid):
    module = import_module('src.core.plugin_market.config')
    (tmp_path / 'plugin_market.toml').write_text(f'[[sources]]\nname="测试"\nindex_url="{url}"\n', encoding='utf-8')
    if valid:
        assert module.load_plugin_market_config(tmp_path).sources[0].index_url == url
    else:
        with pytest.raises(ValueError, match='index_url'):
            module.load_plugin_market_config(tmp_path)
