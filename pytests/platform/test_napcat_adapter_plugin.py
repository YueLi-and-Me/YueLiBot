"""YueLi-NapCat-Adapter 插件验收：清单校验与 poke 探测的失败方向。

探测不连真实协议端——用按剧本应答的传输替身换掉 ``OneBot11Transport``，覆盖
协议端拒绝（status=failed）、响应超时、连接断开、未知 action 四种失败形态，
以及 packet 后端可用的成功形态。所有失败形态的方向只能是「不可用」：
探测失败当可用正是本任务要消灭的故障（戳一戳恒定失败却被放进动作集，
对方收到彻底的沉默）。

"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import asyncio
import importlib.util
import json

import pytest
from structlog.testing import capture_logs

from src.plugin_system import (
    ALL_CAPABILITIES,
    CapabilityError,
    ManifestError,
    load_manifest,
    manifest_from_payload,
)
from src.platforms.onebot11.transport import ActionError, TransportDisconnected


_ROOT = Path(__file__).resolve().parents[2]
_ADAPTER_DIR = _ROOT / 'adapters' / 'yueli-napcat-adapter'
_MANIFEST_PATH = _ADAPTER_DIR / '_manifest.json'
# 协议端在 QQ 自动更新后返回的真实失败原文形态（2026-08-31 现场记录）。
_FAILED_WORDING = 'PacketBackend 不支持当前QQ版本架构：9.9.33-52230-x64'


def _load_plugin_module() -> Any:
    """按宿主的方式从文件路径加载插件模块：目录名带连字符，不能走包导入。"""
    spec = importlib.util.spec_from_file_location(
        'yueli_napcat_adapter_plugin', _ADAPTER_DIR / 'plugin.py',
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubTransport:
    """按构造时给定的剧本响应 call_action 的传输替身；不触碰网络。

    真实 ``OneBot11Transport.call_action`` 在响应 status 非 ok 时抛
    ``ActionError``、等待超时抛 ``asyncio.TimeoutError``、连接断开抛
    ``TransportDisconnected``；替身保持同一边界行为，插件面对的是与真机
    一致的失败形态。
    """

    def __init__(
        self,
        response: Optional[Mapping[str, Any]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self._response = response
        self._error = error
        self.actions: list[str] = []
        self.close_calls = 0

    async def call_action(
        self,
        action: str,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.actions.append(action)
        if self._error is not None:
            raise self._error
        assert self._response is not None, '替身缺少剧本响应'
        return dict(self._response)

    async def close(self) -> None:
        self.close_calls += 1


def _make_plugin(transport: _StubTransport, **overrides: Any) -> Any:
    """用真实清单与替身传输构造插件实例。"""
    module = _load_plugin_module()
    manifest = load_manifest(_MANIFEST_PATH)
    return module.NapCatAdapterPlugin(manifest, transport=transport, **overrides)


async def test_probe_status_failed_excludes_poke_and_logs_protocol_wording() -> None:
    """★1 协议端明确拒绝（status=failed）：poke 不可用，日志带协议端原文。"""
    error = ActionError('nc_get_packet_status', {
        'status': 'failed',
        'retcode': 1400,
        'message': 'packetBackend不可用，请检查后端实现或重试',
        'wording': _FAILED_WORDING,
    })
    plugin = _make_plugin(_StubTransport(error=error))

    with capture_logs() as logs:
        result = await plugin.probe_capabilities()

    assert 'poke' not in result
    warnings = [entry for entry in logs if entry.get('log_level') == 'warning']
    assert warnings, '探测被协议端拒绝必须记 warning'
    assert any(_FAILED_WORDING in str(entry.get('error', '')) for entry in warnings), (
        'warning 里必须带协议端返回的 message/wording 原文'
    )


async def test_probe_timeout_excludes_poke() -> None:
    """★2 探测超时：按不可用处理，不得当成可用。"""
    plugin = _make_plugin(_StubTransport(error=asyncio.TimeoutError()))

    assert await plugin.probe_capabilities() == frozenset()


async def test_probe_disconnect_excludes_poke() -> None:
    """★2 探测期间连接断开：按不可用处理。"""
    plugin = _make_plugin(_StubTransport(error=TransportDisconnected('协议端连接已断开')))

    assert await plugin.probe_capabilities() == frozenset()


async def test_probe_unknown_action_excludes_poke() -> None:
    """★2 协议端没有这个动作（未知 action）：按不可用处理。"""
    error = ActionError('nc_get_packet_status', {
        'status': 'failed',
        'retcode': 1404,
        'message': 'unsupported action',
        'wording': '协议端不支持该 action',
    })
    plugin = _make_plugin(_StubTransport(error=error))

    assert await plugin.probe_capabilities() == frozenset()


async def test_probe_success_includes_poke() -> None:
    """★3 packet 后端可用（status=ok）：poke 进入探测结果。"""
    transport = _StubTransport(response={
        'status': 'ok',
        'retcode': 0,
        'data': {'backend': 'packet'},
    })
    plugin = _make_plugin(transport)

    result = await plugin.probe_capabilities()

    assert result == frozenset({'poke'})
    assert transport.actions == ['nc_get_packet_status']


async def test_resolve_capabilities_probe_success_unions_static_and_poke() -> None:
    """结算方向：探测通过时最终能力是静态声明并上 poke。"""
    plugin = _make_plugin(_StubTransport(response={'status': 'ok', 'retcode': 0}))

    resolved = await plugin.resolve_capabilities()

    assert resolved == plugin.manifest.static_capabilities | {'poke'}


async def test_resolve_capabilities_probe_failure_keeps_static_only() -> None:
    """结算方向：探测失败时最终能力只剩静态声明，收窄而不扩大。"""
    plugin = _make_plugin(_StubTransport(error=asyncio.TimeoutError()))

    resolved = await plugin.resolve_capabilities()

    assert resolved == plugin.manifest.static_capabilities
    assert 'poke' not in resolved


def test_manifest_passes_contract_validation() -> None:
    """★4 真实清单通过契约层校验，且声明内容与任务规格逐字一致。"""
    manifest = load_manifest(_MANIFEST_PATH)

    assert manifest.plugin_id == 'yueli.napcat-adapter'
    assert manifest.config_section == 'napcat'
    assert manifest.protocol == 'onebot11'
    assert manifest.static_capabilities == frozenset({
        'send_message', 'quote_reply', 'reaction',
        'forward_message', 'group_history', 'member_info',
    })
    assert manifest.probed_capabilities == frozenset({'poke'})
    assert manifest.declared_capabilities <= ALL_CAPABILITIES


def _manifest_payload(**overrides: Any) -> Dict[str, Any]:
    """以真实清单为底构造反例，保证反例只违反被测的那一条。"""
    payload = json.loads(_MANIFEST_PATH.read_text(encoding='utf-8'))
    payload.update(overrides)
    return payload


def test_manifest_validation_rejects_static_probed_overlap() -> None:
    """★4 反例：同一能力既静态又待探测，校验必须报错。"""
    payload = _manifest_payload(capabilities={'static': ['poke'], 'probed': ['poke']})

    with pytest.raises(ManifestError):
        manifest_from_payload(payload)


def test_manifest_validation_rejects_capability_outside_enum() -> None:
    """★4 反例：能力标识不在封闭枚举内，校验必须报错。"""
    payload = _manifest_payload(
        capabilities={'static': ['send_massage'], 'probed': ['poke']},
    )

    with pytest.raises(CapabilityError):
        manifest_from_payload(payload)


def test_manifest_validation_rejects_blank_config_section() -> None:
    """★4 反例：config_section 为空白，校验必须报错。"""
    payload = _manifest_payload(config_section='   ')

    with pytest.raises(ManifestError):
        manifest_from_payload(payload)


async def test_on_load_builds_runner_without_network_io(tmp_path: Path) -> None:
    """on_load 只读配置与建对象：全过程不得发起任何协议调用。"""
    config_path = tmp_path / 'napcat.toml'
    config_path.write_text(
        '[inner]\nversion = "0.1.0"\n\n'
        '[napcat]\n'
        'enabled = false\n'
        'host = "127.0.0.1"\n'
        'port = 3001\n'
        'token = ""\n'
        'reconnect_interval_sec = 5\n'
        'action_timeout_sec = 10\n\n'
        '[owner]\n'
        'qq = ""\n',
        encoding='utf-8',
    )
    runtime_path = tmp_path / 'backend.json'
    runtime_path.write_text(
        json.dumps({'port': 18080, 'token': 'ab' * 32}),
        encoding='utf-8',
    )
    transport = _StubTransport(response={'status': 'ok', 'retcode': 0})
    plugin = _make_plugin(
        transport, config_path=config_path, runtime_path=runtime_path,
    )

    await plugin.on_load()

    assert transport.actions == [], 'on_load 禁止任何协议调用（网络 I/O）'
    # on_load 之后探测走的是同一个传输实例，而不是插件另起的连接。
    assert await plugin.probe_capabilities() == frozenset({'poke'})


async def test_on_stop_is_idempotent() -> None:
    """on_stop 幂等：停机与重连两条路径都会调用，重复调用不得报错。"""
    transport = _StubTransport()
    plugin = _make_plugin(transport)

    await plugin.on_stop()
    await plugin.on_stop()

    assert transport.close_calls == 2
