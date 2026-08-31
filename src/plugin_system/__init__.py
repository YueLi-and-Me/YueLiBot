"""适配器插件契约层。

对外暴露三样东西：能力封闭枚举、清单模型与加载函数、适配器插件基类。具体适配器包
（``adapters/``）依赖本包，本包不反向依赖任何适配器，也不含协议实现。
"""

from .adapter import AdapterPlugin
from .capabilities import (
    ALL_CAPABILITIES,
    AdapterCapability,
    CapabilityError,
    parse_capabilities,
)
from .manifest import (
    SUPPORTED_MANIFEST_VERSION,
    AdapterManifest,
    ManifestError,
    load_manifest,
    manifest_from_payload,
)


__all__ = [
    'ALL_CAPABILITIES',
    'SUPPORTED_MANIFEST_VERSION',
    'AdapterCapability',
    'AdapterManifest',
    'AdapterPlugin',
    'CapabilityError',
    'ManifestError',
    'load_manifest',
    'manifest_from_payload',
    'parse_capabilities',
]
