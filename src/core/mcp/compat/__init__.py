"""兼容层门面；对外只提供自有数据、连接句柄与少数操作函数。"""

from .types import (
    McpContentItem, McpFailure, McpServerInfo, McpTool, McpToolResult, McpUnavailableError,
    PROTOCOL_VERSION_MAX, PROTOCOL_VERSION_MIN, SDK_VERSION_RANGE, SECRET_MIN_CHARS, connection_secrets,
)
from .sdk import McpConnection, check_sdk_version, connect

__all__ = ['McpConnection', 'McpContentItem', 'McpFailure', 'McpServerInfo', 'McpTool',
           'McpToolResult', 'McpUnavailableError', 'connect', 'check_sdk_version',
           'SDK_VERSION_RANGE', 'PROTOCOL_VERSION_MIN', 'PROTOCOL_VERSION_MAX',
           'SECRET_MIN_CHARS', 'connection_secrets']
