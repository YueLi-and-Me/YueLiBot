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
from .loader import PluginLoadError, load_adapter_plugin
from .manifest import (
    ALL_PLUGIN_TYPES,
    SUPPORTED_MANIFEST_VERSION,
    AdapterManifest,
    ManifestError,
    PluginManifest,
    PluginType,
    load_manifest,
    manifest_from_payload,
)
from .plugin import Plugin
from .tools import ToolPlugin, tool


__all__ = [
    'ALL_CAPABILITIES',
    'ALL_PLUGIN_TYPES',
    'SUPPORTED_MANIFEST_VERSION',
    'AdapterCapability',
    'AdapterManifest',
    'AdapterPlugin',
    'CapabilityError',
    'ManifestError',
    'Plugin',
    'PluginLoadError',
    'PluginManifest',
    'PluginType',
    'ToolPlugin',
    'load_adapter_plugin',
    'load_manifest',
    'manifest_from_payload',
    'parse_capabilities',
    'tool',
]
