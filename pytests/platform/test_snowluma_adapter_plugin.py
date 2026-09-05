"""验证 YueLi-SnowLuma-Adapter 插件的清单声明与生命周期契约。

本文件全部用替身驱动，不连接真实协议端：清单断言只走契约层校验函数，
生命周期断言使用记录调用次数的传输与主体替身。规格已实测过真实协议端，
此处不重复实测，更不允许触发任何有副作用的协议动作。

依赖 ``src.plugin_system`` 与 ``adapters/yueli-snowluma-adapter``。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from src.platforms.onebot11.runner import OneBot11Runner
from src.plugin_system import (
    ALL_CAPABILITIES,
    ManifestError,
    load_manifest,
    manifest_from_payload,
)

_ADAPTER_DIR = Path(__file__).resolve().parents[2] / 'adapters' / 'yueli-snowluma-adapter'
_MANIFEST_PATH = _ADAPTER_DIR / '_manifest.json'
_PLUGIN_PATH = _ADAPTER_DIR / 'plugin.py'


@functools.lru_cache(maxsize=1)
def _plugin_class() -> type:
    """按文件路径加载插件类；目录名含连字符，不能走常规包导入。

    加载放在用例内而不是模块顶层，改动前（插件未建立时）每个用例各自失败，
    保留逐条断言的失败证据。
    """
    spec = importlib.util.spec_from_file_location('yueli_snowluma_adapter_plugin', _PLUGIN_PATH)
    assert spec is not None and spec.loader is not None, f'无法加载插件模块：{_PLUGIN_PATH}'
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SnowlumaAdapterPlugin

_RUNTIME_TOKEN = 'a' * 64


class _ZeroCallTransport:
    """替身协议传输：记录全部方法调用，用于断言插件未发起任何网络调用。"""

    def __init__(self, config: object) -> None:
        self.config = config
        self.calls: list[str] = []

    async def connect(self) -> str:
        self.calls.append('connect')
        raise AssertionError('替身传输不应被连接')

    async def close(self) -> None:
        self.calls.append('close')


class _HangingTransport:
    """替身协议传输：connect 永久挂起，模拟占用收发循环的长连接。"""

    def __init__(self, config: object) -> None:
        self.config = config
        self.connect_attempts = 0
        self.close_count = 0

    async def connect(self) -> str:
        self.connect_attempts += 1
        await asyncio.Future()

    async def close(self) -> None:
        self.close_count += 1


class _HangingBackend:
    """替身主体客户端：连接挂起，只统计关闭次数。"""

    def __init__(self) -> None:
        self.close_count = 0

    async def connect(self) -> None:
        await asyncio.Future()

    async def close(self) -> None:
        self.close_count += 1


def _write_adapter_config(tmp_path: Path, *, section: str = 'snowluma') -> Path:
    """写一份适配器配置文件；section 参数用于构造段名写错的反例。"""
    path = tmp_path / 'snowluma.toml'
    path.write_text(
        '[inner]\nversion = "0.1.0"\n\n'
        f'[{section}]\n'
        'enabled = true\n'
        'self_qq = 13579\n'
        'host = "127.0.0.1"\n'
        'port = 3001\n'
        'token = "snowluma-token"\n'
        'reconnect_interval_sec = 5\n'
        'action_timeout_sec = 15\n\n'
        '[owner]\nqq = "24680"\n',
        encoding='utf-8',
    )
    return path


def _write_runtime(tmp_path: Path, *, port: int = 8765) -> Path:
    path = tmp_path / 'backend.json'
    path.write_text(
        json.dumps({'port': port, 'token': _RUNTIME_TOKEN}),
        encoding='utf-8',
    )
    return path


def _build_plugin(tmp_path: Path, *, section: str = 'snowluma', transport: Any = None, backend: Any = None):
    manifest = load_manifest(_MANIFEST_PATH)
    return _plugin_class()(
        manifest,
        config_path=_write_adapter_config(tmp_path, section=section),
        runtime_path=_write_runtime(tmp_path),
        transport=transport,
        backend=backend,
    )


# ---------------------------------------------------------------- ★1 清单


def test_清单通过契约层校验且能力全部静态声明() -> None:
    manifest = load_manifest(_MANIFEST_PATH)

    assert manifest.plugin_id == 'yueli.snowluma-adapter'
    assert manifest.protocol == 'onebot11'
    assert manifest.config_section == 'snowluma'
    assert manifest.static_capabilities == ALL_CAPABILITIES
    assert manifest.probed_capabilities == frozenset()


def test_静态与待探测有交集时清单校验必须报错() -> None:
    payload: dict = json.loads(_MANIFEST_PATH.read_text(encoding='utf-8'))
    payload['capabilities'] = {
        'static': ['poke'],
        'probed': ['poke'],
    }

    with pytest.raises(ManifestError, match='static 与 probed'):
        manifest_from_payload(payload)


# ---------------------------------------------------------------- ★2 探测


async def test_探测返回空集合且不发起任何网络调用(tmp_path: Path) -> None:
    transport = _ZeroCallTransport(None)
    plugin = _build_plugin(tmp_path, transport=transport)

    await plugin.on_load()
    probed = await plugin.probe_capabilities()

    assert probed == frozenset()
    assert transport.calls == []


async def test_能力结算结果等于静态集合(tmp_path: Path) -> None:
    plugin = _build_plugin(tmp_path, transport=_ZeroCallTransport(None))
    await plugin.on_load()

    resolved = await plugin.resolve_capabilities()

    assert resolved == ALL_CAPABILITIES


# ---------------------------------------------------------------- ★3 加载


async def test_on_load读取snowluma段成功构造运行器(tmp_path: Path) -> None:
    transport = _ZeroCallTransport(None)
    plugin = _build_plugin(tmp_path, transport=transport)

    await plugin.on_load()

    runner = plugin._runner
    assert isinstance(runner, OneBot11Runner)
    assert runner._config.napcat.host == '127.0.0.1'
    assert runner._config.napcat.port == 3001
    assert runner._config.napcat.token == 'snowluma-token'
    assert runner._config.napcat.self_qq == '13579'
    assert runner._config.owner.qq == '24680'
    assert runner._backend_port == 8765
    assert runner._token == _RUNTIME_TOKEN
    # 加载只构造对象，不建立连接。
    assert transport.calls == []


async def test_on_load段缺失时报出明确错误(tmp_path: Path) -> None:
    # 段名写成另一个适配器的 napcat，对应「配置抄错模板」的真实失误形态。
    plugin = _build_plugin(tmp_path, section='napcat')

    with pytest.raises(ValueError, match='snowluma') as exc_info:
        await plugin.on_load()

    assert 'snowluma.toml' in str(exc_info.value)
    assert plugin._runner is None


# ---------------------------------------------------------------- ★4 停机


async def test_on_stop连续调用两次不抛异常(tmp_path: Path) -> None:
    transport = _HangingTransport(None)
    plugin = _build_plugin(tmp_path, transport=transport, backend=_HangingBackend())

    await plugin.on_load()
    # on_start 阻塞到停机，所以由测试自己起任务——宿主也是这么做的。
    task = asyncio.create_task(plugin.on_start())
    await asyncio.sleep(0)
    assert transport.connect_attempts == 1

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await plugin.on_stop()
    await plugin.on_stop()

    assert transport.close_count >= 1


async def test_on_stop先于on_start调用不抛异常(tmp_path: Path) -> None:
    plugin = _build_plugin(tmp_path, transport=_HangingTransport(None))

    await plugin.on_load()
    await plugin.on_stop()


async def test_on_start先于on_load调用时报错(tmp_path: Path) -> None:
    plugin = _build_plugin(tmp_path, transport=_ZeroCallTransport(None))

    with pytest.raises(RuntimeError, match='on_load'):
        await plugin.on_start()
