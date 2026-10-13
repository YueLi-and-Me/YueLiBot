"""校验插件版本判定、发现边界与状态账本。

使用临时插件目录验证入口执行前的版本限制，日志由结构化日志捕获器核对。
"""

from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import Mock
import json

import pytest

from src.plugin_system.manifest import SUPPORTED_MANIFEST_VERSION
from src.plugin_system.registry import PluginRegistry


def write_plugin(root: Path, name: str, bounds: Optional[Dict[str, str]] = None,
                 source: str = 'from src.plugin_system import ToolPlugin\nclass Example(ToolPlugin):\n    pass\n') -> Path:
    """创建不依赖模型或网络的插件；区间缺省时清单不声明宿主版本。"""
    directory = root / name
    directory.mkdir(parents=True)
    payload: Dict[str, Any] = {
        'manifest_version': SUPPORTED_MANIFEST_VERSION, 'id': 'test.' + name,
        'plugin_type': 'tool', 'name': name, 'version': '1.0.0', 'description': '测试工具',
    }
    if bounds is not None:
        payload['host_application'] = bounds
    (directory / '_manifest.json').write_text(json.dumps(payload), encoding='utf-8')
    (directory / 'plugin.py').write_text(source, encoding='utf-8')
    return directory


def test_missing_range_is_rejected_only_for_third_party(tmp_path: Path) -> None:
    """第三方缺上界必须跳过；内置同形清单仍能加载。"""
    write_plugin(tmp_path / 'external', 'external', {'min_version': '0.0.0'})
    write_plugin(tmp_path / 'builtin', 'builtin', {'min_version': '0.0.0'})
    registry = PluginRegistry()
    registry.discover([(tmp_path / 'builtin', False), (tmp_path / 'external', True)])
    assert [p.manifest.name for p in registry.tool_plugins()] == ['builtin']
    status = next(s for s in registry.statuses() if s.name == 'external')
    assert status.decision == '不兼容'
    assert status.reason == '插件 external 未声明支持的主程序版本区间（min_version 与 max_version 均必填），已跳过'


@pytest.mark.parametrize('current,decision', [('0.2.3', '兼容模式'), ('0.3.0', '不兼容'), ('0.1.9', '不兼容')])
def test_version_decision_and_warning_once(tmp_path: Path, monkeypatch, current: str, decision: str) -> None:
    """补丁越界可加载且进程只告警一次，主次版本及低于下界必须拒绝。"""
    from src.plugin_system.manifest import check_host_compatibility, load_manifest
    monkeypatch.setattr('src.plugin_system.manifest.APP_VERSION', current)
    directory = write_plugin(tmp_path, 'versioned', {'min_version': '0.2.0', 'max_version': '0.2.0'})
    manifest = load_manifest(directory / '_manifest.json')
    assert check_host_compatibility(manifest, third_party=True, force=False).decision == decision
    log = Mock()
    monkeypatch.setattr('src.plugin_system.manifest.logger', log)
    registry = PluginRegistry()
    registry.discover([(tmp_path, True)])
    registry.discover([(tmp_path, True)])
    assert len(registry.tool_plugins()) == int(decision != '不兼容')
    if decision == '兼容模式':
        assert sum('按兼容模式加载' in str(c) for c in log.warning.call_args_list) == 1


@pytest.mark.parametrize('current,bounds', [
    ('0.2.3', {'min_version': '0.2.0'}),
    ('0.3.0', {'min_version': '0.2.0', 'max_version': '0.2.0'}),
    ('0.1.9', {'min_version': '0.2.0', 'max_version': '0.2.0'}),
])
def test_force_compatibility_loads_rejected_plugins(tmp_path: Path, monkeypatch, current: str, bounds: Dict[str, str]) -> None:
    """强制兼容改变版本拒绝判定，并明确记录放行原因。"""
    monkeypatch.setattr('src.plugin_system.manifest.APP_VERSION', current)
    write_plugin(tmp_path, 'forced_' + current.replace('.', '_'), bounds)
    registry = PluginRegistry(force_compatibility=True)
    registry.discover([(tmp_path, True)])
    assert len(registry.tool_plugins()) == 1
    status = registry.statuses()[0]
    assert status.decision == '强制兼容'
    assert status.reason.startswith('已开启强制兼容：插件 ')


async def test_status_ledger_covers_every_decision(tmp_path: Path, monkeypatch) -> None:
    """发现与生命周期失败都保留目录级状态，不因移除实例而消失。"""
    monkeypatch.setattr('src.plugin_system.manifest.APP_VERSION', '0.2.3')
    normal = {'min_version': '0.2.0', 'max_version': '0.2.9'}
    write_plugin(tmp_path, 'normal', normal)
    write_plugin(tmp_path, 'compatible', {'min_version': '0.2.0', 'max_version': '0.2.0'})
    write_plugin(tmp_path, 'incompatible')
    disabled = write_plugin(tmp_path, 'disabled', normal)
    (disabled / 'config.toml').write_text('[plugin]\nenabled = false\n', encoding='utf-8')
    malformed = write_plugin(tmp_path, 'malformed', normal)
    (malformed / '_manifest.json').write_text('{', encoding='utf-8')
    write_plugin(tmp_path, 'failed', normal,
                 'from src.plugin_system import ToolPlugin\nclass Example(ToolPlugin):\n    async def on_load(self):\n        raise RuntimeError("加载失败原因")\n')
    registry = PluginRegistry()
    registry.discover([(tmp_path, True)])
    await registry.load_all()
    forced_root = tmp_path / 'forced-root'
    write_plugin(forced_root, 'forced')
    forced = PluginRegistry(force_compatibility=True)
    forced.discover([(forced_root, True)])
    statuses = registry.statuses() + forced.statuses()
    assert {s.decision for s in statuses} == {'正常', '兼容模式', '强制兼容', '不兼容', '已停用', '清单错误', '加载失败'}
    assert len(statuses) == 7
    assert next(s for s in statuses if s.name == 'failed').reason == '加载失败原因'
