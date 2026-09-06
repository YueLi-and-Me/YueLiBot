"""插件契约层。

对外暴露四样东西：能力封闭枚举、清单模型与加载函数、插件基类（适配器与工具）、
插件注册表。具体适配器包（``adapters/``）与工具插件依赖本包，本包不反向依赖任何
插件，也不含协议实现。
"""

from .adapter import AdapterPlugin
from .config import PluginConfig
from .capabilities import (
    ALL_CAPABILITIES,
    AdapterCapability,
    CapabilityError,
    parse_capabilities,
)
from .loader import PluginLoadError, load_adapter_plugin, load_tool_plugin
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
from .registry import PluginRegistry
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
    'PluginConfig',
    'PluginLoadError',
    'PluginManifest',
    'PluginRegistry',
    'PluginType',
    'ToolPlugin',
    'load_adapter_plugin',
    'load_manifest',
    'load_tool_plugin',
    'manifest_from_payload',
    'parse_capabilities',
    'tool',
]
