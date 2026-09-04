"""开发者命令通道的注册、匹配与执行入口。"""

from .registry import (
    CommandCatalogItem,
    CommandContext,
    CommandDispatch,
    CommandSpec,
    dispatch_developer_command,
    register_command,
    registered_commands,
)

__all__ = [
    'CommandCatalogItem',
    'CommandContext',
    'CommandDispatch',
    'CommandSpec',
    'dispatch_developer_command',
    'register_command',
    'registered_commands',
]
