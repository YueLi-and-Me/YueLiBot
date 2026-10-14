"""提供工具插件访问宿主的窄视图，避免插件依赖完整配置与内核服务。

宿主用 Config 构造 PluginContext，再通过 Plugin.bind_context 注入。
插件读取 logger、paths、host 与 MCP 窄入口；配置只复制三个已约定字段，不保留 Config 引用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING

from src.core.config.schema import Config
from src.core.logging.logger import get_logger
from src.core.mcp.compat.types import McpContentItem, McpFailure, McpToolResult, McpUnavailableError

if TYPE_CHECKING:
    from src.core.mcp.manager import McpManager


class PluginMcpAccess:
    """只暴露调用配置中只读工具的入口，权限、计数与连接全部由宿主管理器处理。"""

    __slots__ = ('_manager',)

    def __init__(self, manager: Optional['McpManager']) -> None:
        """绑定宿主服务；未装配时仍提供明确的中文不可用错误。"""
        self._manager = manager

    async def call_tool(self, server: str, tool: str, arguments: Dict[str, Any]) -> McpToolResult:
        """调用已列入配置的只读工具，expose 不影响插件调用授权。

        :param server: 已配置服务名。
        :param tool: 服务上的原始工具名。
        :param arguments: 按服务参数结构提供的参数对象。
        :return: 统一结果；工具、超时或服务器错误保留为 is_error=True。
        :raises McpUnavailableError: 未授权、未连上、非只读或今日达到上限。
        """
        if self._manager is None:
            raise McpUnavailableError('宿主 MCP 未启用')
        try:
            return await self._manager.call_tool(server, tool, arguments)
        except McpFailure as exc:
            return McpToolResult(True, (McpContentItem('text', text=str(exc)),))


@dataclass(frozen=True)
class HostView:
    """插件可见的宿主配置窄视图。

    刻意只有三个字段：ctx 暴露出去的每一样都是对插件作者的承诺，加进来容易拿掉难。
    需要更多字段时单独议，不要在实现里顺手加。
    """

    bot_name: str
    https_proxy: str
    data_dir: Path


@dataclass(frozen=True)
class PluginPaths:
    """插件可用的两个目录，不负责创建目录。

    plugin_dir 是插件自己的目录，config.toml 就在这里；data_dir 是宿主数据目录，
    插件要落盘时在其下自建子目录。
    """

    plugin_dir: Path
    data_dir: Path


class PluginContext:
    """插件访问宿主的唯一入口。插件不得直接 import src.core 下的任何模块。"""

    __slots__ = ('_logger', '_paths', '_host', '_mcp')

    def __init__(self, plugin_id: str, plugin_dir: Path, data_dir: Path, cfg: Config,
                 mcp: Optional['McpManager'] = None) -> None:
        """从宿主配置生成只读快照，MCP 服务只通过窄入口提供，不保留完整配置。

        :param plugin_id: 已校验清单中的插件标识，用于日志归属。
        :param plugin_dir: 插件目录，包含入口与插件配置。
        :param data_dir: 宿主确定的数据根目录，与 host.data_dir 使用同一个值。
        :param cfg: 宿主已校验配置，仅取 bot.name 与 advanced.https_proxy。
        :param mcp: 宿主 MCP 管理器；未提供时调用会抛出中文不可用错误。
        副作用：构造日志代理；不创建目录，不读写磁盘。
        """
        self._logger = get_logger(f'src.plugin_system.context.{plugin_id}')
        self._mcp = PluginMcpAccess(mcp)
        self._paths = PluginPaths(plugin_dir=plugin_dir, data_dir=data_dir)
        self._host = HostView(
            bot_name=cfg.bot.name,
            https_proxy=cfg.advanced.https_proxy,
            data_dir=data_dir,
        )

    @property
    def logger(self) -> Any:
        """结构化日志器，已绑定本插件的 id。"""
        return self._logger

    @property
    def paths(self) -> 'PluginPaths':
        """插件目录与数据目录。"""
        return self._paths

    @property
    def host(self) -> HostView:
        """宿主配置的只读窄视图。"""
        return self._host

    @property
    def mcp(self) -> PluginMcpAccess:
        """调用部署者列出的只读 MCP 工具，与模型调用共用每日次数。"""
        return self._mcp
