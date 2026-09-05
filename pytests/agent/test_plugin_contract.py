"""验证适配器插件契约：能力枚举、清单校验与能力结算方向。

契约层是三个下游适配器任务的唯一依据，因此这里锁死的是**语义**而非实现细节：
能力标识封闭、清单矛盾必须在加载期暴露、能力结算只能收窄不能扩大、探测失败按不可用。

依赖 ``src.plugin_system``。
"""

from __future__ import annotations

from typing import FrozenSet

import pytest

from src.plugin_system import (
    AdapterCapability,
    AdapterManifest,
    AdapterPlugin,
    CapabilityError,
    ManifestError,
    manifest_from_payload,
)


def _payload(**overrides: object) -> dict:
    """构造一份合法清单，便于逐字段构造反例。"""
    payload = {
        'manifest_version': 1,
        'id': 'yueli.sample-adapter',
        'plugin_type': 'adapter',
        'name': 'YueLi-Sample-Adapter',
        'version': '0.1.0',
        'description': '测试用适配器',
        'protocol': 'onebot11',
        'config_section': 'sample',
        'capabilities': {'static': ['send_message'], 'probed': ['poke']},
    }
    payload.update(overrides)
    return payload


class _StubAdapter(AdapterPlugin):
    """按构造参数决定探测行为的适配器替身。"""

    def __init__(
        self,
        manifest: AdapterManifest,
        probe_result: FrozenSet[AdapterCapability] | None = None,
        probe_error: Exception | None = None,
    ) -> None:
        """记录探测应当返回什么或抛什么，并统计探测调用次数。"""
        super().__init__(manifest)
        self._probe_result = probe_result if probe_result is not None else frozenset()
        self._probe_error = probe_error
        self.probe_calls = 0

    async def on_load(self) -> None:
        """测试不涉及加载。"""

    async def probe_capabilities(self) -> FrozenSet[AdapterCapability]:
        """按构造参数返回或抛出。"""
        self.probe_calls += 1
        if self._probe_error is not None:
            raise self._probe_error
        return self._probe_result

    async def on_start(self) -> None:
        """测试不涉及启动。"""

    async def on_stop(self) -> None:
        """测试不涉及停止。"""


def test_manifest_accepts_wellformed_payload() -> None:
    """合法清单解析出的能力上界等于静态与待探测之并。"""
    manifest = manifest_from_payload(_payload())

    assert manifest.plugin_id == 'yueli.sample-adapter'
    assert manifest.config_section == 'sample'
    assert manifest.declared_capabilities == frozenset({'send_message', 'poke'})


def test_manifest_rejects_unknown_capability() -> None:
    """拼错的能力标识必须在加载期暴露，而不是表现为该能力永远不可用。"""
    payload = _payload(capabilities={'static': ['send_massage'], 'probed': []})

    with pytest.raises(CapabilityError, match='不是合法能力'):
        manifest_from_payload(payload)


def test_manifest_rejects_static_and_probed_overlap() -> None:
    """同一能力不能既静态声明又待探测，否则探测结果会被静态声明覆盖。"""
    payload = _payload(capabilities={'static': ['poke'], 'probed': ['poke']})

    with pytest.raises(ManifestError, match='不能同时出现'):
        manifest_from_payload(payload)


def test_manifest_rejects_wrong_version() -> None:
    """清单格式版本必须逐字相等：语义变化不允许静默按旧版运行。"""
    with pytest.raises(ManifestError, match='清单格式版本'):
        manifest_from_payload(_payload(manifest_version=2))


def test_manifest_requires_config_section() -> None:
    """缺少配置段名会让两个适配器无法区分各自的连接参数。"""
    with pytest.raises(ManifestError, match='config_section'):
        manifest_from_payload(_payload(config_section='   '))


@pytest.mark.asyncio
async def test_probe_success_adds_only_probed_capability() -> None:
    """探测通过时，最终能力等于静态并上探测确认的部分。"""
    manifest = manifest_from_payload(_payload())
    adapter = _StubAdapter(manifest, probe_result=frozenset({'poke'}))

    assert await adapter.resolve_capabilities() == frozenset({'send_message', 'poke'})


@pytest.mark.asyncio
async def test_probe_failure_falls_to_unavailable_not_available() -> None:
    """探测抛异常时待探测能力整体按不可用——不可用是安全方向。"""
    manifest = manifest_from_payload(_payload())
    adapter = _StubAdapter(manifest, probe_error=RuntimeError('协议端拒绝'))

    assert await adapter.resolve_capabilities() == frozenset({'send_message'})


@pytest.mark.asyncio
async def test_probe_negative_result_removes_capability() -> None:
    """探测判定不可用时该能力不进入最终集合。"""
    manifest = manifest_from_payload(_payload())
    adapter = _StubAdapter(manifest, probe_result=frozenset())

    assert await adapter.resolve_capabilities() == frozenset({'send_message'})


@pytest.mark.asyncio
async def test_probe_cannot_invent_undeclared_capability() -> None:
    """探测返回清单未声明的能力属于装配错误，必须暴露而不是过滤掉。"""
    manifest = manifest_from_payload(_payload())
    adapter = _StubAdapter(manifest, probe_result=frozenset({'poke', 'reaction'}))

    with pytest.raises(ValueError, match='未声明的能力'):
        await adapter.resolve_capabilities()


@pytest.mark.asyncio
async def test_no_probed_capabilities_skips_probe_entirely() -> None:
    """清单没有待探测能力时不调用探测，避免无谓的网络往返。"""
    payload = _payload(capabilities={'static': ['send_message'], 'probed': []})
    adapter = _StubAdapter(manifest_from_payload(payload))

    result = await adapter.resolve_capabilities()

    assert result == frozenset({'send_message'})
    assert adapter.probe_calls == 0
