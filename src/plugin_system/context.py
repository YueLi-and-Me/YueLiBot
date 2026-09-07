"""插件访问宿主的唯一入口（签名占位件）。

本文件只交付签名与文档：字段齐全、方法体一律 ``NotImplementedError``。
宿主接线由契约地基一侧完成，届时按整文件替换本占位件，不做行级合并；
在此之前，用例通过 ``bind_context`` 注入桩件驱动依赖它的组件。

插件不得直接 import ``src.core`` 下的任何模块；需要宿主的目录、配置窄视图或
日志器时，一律经 ``self.ctx`` 取用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    """插件可用的两个目录。"""

    plugin_dir: Path      # 插件自己的目录，config.toml 就在这里
    data_dir: Path        # 宿主数据目录，插件要落盘时在其下自建子目录


class PluginContext:
    """插件访问宿主的唯一入口。插件不得直接 import src.core 下的任何模块。"""

    @property
    def logger(self) -> Any:
        """结构化日志器，已绑定本插件的 id。"""
        raise NotImplementedError

    @property
    def paths(self) -> 'PluginPaths':
        """插件目录与数据目录。"""
        raise NotImplementedError

    @property
    def host(self) -> HostView:
        """宿主配置的只读窄视图。"""
        raise NotImplementedError
