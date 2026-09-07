"""提供工具插件访问宿主的窄视图，避免插件依赖完整配置与内核服务。

宿主用 Config 构造 PluginContext，再通过 Plugin.bind_context 注入。
插件只读取 logger、paths、host；配置只复制三个已约定字段，不保留 Config 引用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.config.schema import Config
from src.core.logging.logger import get_logger


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

    __slots__ = ('_logger', '_paths', '_host')

    def __init__(self, plugin_id: str, plugin_dir: Path, data_dir: Path, cfg: Config) -> None:
        """从宿主配置生成只读快照，不持有内核服务或完整配置。

        :param plugin_id: 已校验清单中的插件标识，用于日志归属。
        :param plugin_dir: 插件目录，包含入口与插件配置。
        :param data_dir: 宿主确定的数据根目录，与 host.data_dir 使用同一个值。
        :param cfg: 宿主已校验配置，仅取 bot.name 与 advanced.https_proxy。
        副作用：构造日志代理；不创建目录，不读写磁盘。
        """
        self._logger = get_logger(f'src.plugin_system.context.{plugin_id}')
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
