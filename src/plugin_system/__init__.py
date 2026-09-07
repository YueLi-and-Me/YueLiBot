"""插件契约层。

对外暴露能力封闭枚举、清单模型与加载函数、插件基类（适配器与工具）、插件注册表、
组件声明装饰器（``@tool`` / ``@inbound_rewrite`` / ``@inbound_observe`` / ``@command``），
以及工具协议、入站引用、转发树和宿主上下文这几类数据结构。具体适配器包
（``adapters/``）与工具插件依赖本包，本包不反向依赖任何插件，也不含协议实现。

**工具插件只能从本包获取宿主类型**，这条边界由回归用例扫描源码保证。
``adapters/`` 跑在独立进程、依赖协议核心，适配器的导入边界不在此约束内。
"""

from src.core.commands import CommandContext, CommandReply
from src.core.platform_io.forward import ForwardMessagePart, ForwardMessageTree, ForwardNode
from src.core.platform_io.types import (
    ConversationContext,
    InboundMessage,
    PersonRef,
    StreamKind,
    StreamRef,
)
from src.core.tooling.spec import (
    DEFAULT_TOOL_TIMEOUT_MS,
    ToolContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolSideEffect,
    ToolSpec,
)

from .adapter import AdapterPlugin
from .config import PluginConfig
from .context import HostView, PluginContext, PluginPaths
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
from .tools import ToolPlugin
from .components import command, inbound_observe, inbound_rewrite, tool


__all__ = [
    'ALL_CAPABILITIES',
    'ALL_PLUGIN_TYPES',
    'DEFAULT_TOOL_TIMEOUT_MS',
    'SUPPORTED_MANIFEST_VERSION',
    'AdapterCapability',
    'AdapterManifest',
    'AdapterPlugin',
    'CapabilityError',
    'CommandContext',
    'CommandReply',
    'ConversationContext',
    'ForwardMessagePart',
    'ForwardMessageTree',
    'ForwardNode',
    'HostView',
    'InboundMessage',
    'ManifestError',
    'PersonRef',
    'Plugin',
    'PluginConfig',
    'PluginContext',
    'PluginLoadError',
    'PluginManifest',
    'PluginPaths',
    'PluginRegistry',
    'PluginType',
    'StreamKind',
    'StreamRef',
    'ToolContext',
    'ToolExecutionResult',
    'ToolInvocation',
    'ToolPlugin',
    'ToolSideEffect',
    'ToolSpec',
    'command',
    'inbound_observe',
    'inbound_rewrite',
    'load_adapter_plugin',
    'load_manifest',
    'load_tool_plugin',
    'manifest_from_payload',
    'parse_capabilities',
    'tool',
]
