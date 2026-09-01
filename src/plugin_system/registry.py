"""扫描插件根目录、按类型分派，并把工具插件聚合成主体可查询的集合。

目录约定::

    src/plugins/built_in/<插件名>/     内置插件，随程序发布
    plugins/<插件名>/                  第三方插件，用户自行放置

每个插件目录含 ``_manifest.json`` 与 ``plugin.py``，与适配器一致。两个根目录都扫，
调用方把内置根目录排在前：同 id 冲突时先扫描到的生效，后者被忽略并记 warning。

注册表是主体与工具插件之间的唯一界面：主体只调 ``discover`` / ``load_all`` /
``unload_all`` / ``observe_inbound`` / ``stream_capabilities``，不直接接触单个插件。
隔离原则贯穿全表：第三方插件目录里混着一个坏插件时，整个 Bot 起不来是不可接受的，
因此发现、加载、入站观察、能力查询任何一步的单插件失败都只影响该插件自身。

依赖 ``loader`` 与 ``tools``；被主体（聊天服务）在启动期驱动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Sequence

from src.core.common.logger import get_logger

from .loader import MANIFEST_FILENAME, load_tool_plugin
from .manifest import load_manifest
from .tools import ToolPlugin


logger = get_logger(__name__)


class PluginRegistry:
    """工具插件的注册表：发现、生命周期驱动与聚合查询。

    生命周期固定为 ``discover`` → ``load_all`` → 运行期聚合调用 → ``unload_all``，
    由主体按序驱动。``load_all`` 失败的插件会从注册表移除，此后的聚合调用只覆盖
    加载成功的插件。
    """

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._plugins: List[ToolPlugin] = []
        # 插件 id → 来源目录，用于同 id 冲突时指出被保留者与后来者各自的位置。
        self._origins: Dict[str, Path] = {}
        # 已成功 on_load 的插件；unload_all 只卸载它们，据此保证自身幂等。
        self._loaded: List[ToolPlugin] = []

    def discover(self, roots: Sequence[Path]) -> None:
        """按序扫描插件根目录，加载全部合法的工具插件。

        :param roots: 插件根目录，按优先级从高到低排列——同 id 冲突时先扫描到的
            生效，后者被忽略并记 warning；内置根目录因此应排在第三方之前。不存在
            的根目录直接跳过（用户可能从未放置第三方插件）。
        :return: ``None``。
        副作用：读取各插件目录的清单与入口文件，执行入口模块顶层代码；单个插件
            的任何失败只记 error 并跳过，不中断其余插件的发现。
        """
        for root in roots:
            self._discover_root(root)

    def tool_plugins(self) -> Sequence[ToolPlugin]:
        """返回当前已发现的全部工具插件。

        :return: 插件序列；``load_all`` 失败的插件已从其中移除。
        """
        return tuple(self._plugins)

    async def load_all(self) -> None:
        """依次驱动各插件的 ``on_load``。

        单个插件加载失败记 error、从注册表移除并继续下一个：一个配置写错的第三方
        插件不该让整个 Bot 起不来。重复调用只驱动尚未加载成功的插件。

        :return: ``None``。
        """
        for plugin in list(self._plugins):
            if plugin in self._loaded:
                continue
            try:
                await plugin.on_load()
            except Exception as exc:
                logger.error(
                    '工具插件加载失败，已从注册表移除',
                    plugin=plugin.manifest.plugin_id,
                    error=str(exc),
                )
                self._plugins.remove(plugin)
            else:
                self._loaded.append(plugin)

    async def unload_all(self) -> None:
        """依次驱动已加载插件的 ``on_unload``；幂等，重复调用是空操作。

        单个插件卸载失败记 error 并继续卸载其余插件：停机路径不该被一个插件的
        bug 卡住。

        :return: ``None``。
        """
        while self._loaded:
            plugin = self._loaded.pop()
            try:
                await plugin.on_unload()
            except Exception as exc:
                logger.error(
                    '工具插件卸载失败',
                    plugin=plugin.manifest.plugin_id,
                    error=str(exc),
                )

    def observe_inbound(self, stream_id: int, message_id: int, inbound: Any) -> None:
        """把一条已入库的入站消息分发给全部工具插件观察。

        入站是主链路：单个插件抛异常时记 error 并继续下一个，插件的 bug 不该让
        消息进不来。

        :param stream_id: 会话编号。
        :param message_id: 该消息在主体侧的内部编号。
        :param inbound: 入站消息对象；字段见 ``src.core.platform_io.types``。
        :return: ``None``。
        """
        for plugin in self._plugins:
            try:
                plugin.observe_inbound(stream_id, message_id, inbound)
            except Exception as exc:
                logger.error(
                    '工具插件观察入站消息失败，已跳过该插件本次调用',
                    plugin=plugin.manifest.plugin_id,
                    error=str(exc),
                )

    def stream_capabilities(self, stream_id: int) -> FrozenSet[str]:
        """合并全部工具插件为该会话贡献的能力。

        与 ``observe_inbound`` 同理，单个插件抛异常记 error 并跳过，合并不中断。

        :param stream_id: 会话编号。
        :return: 各插件返回值之并集；没有插件贡献时为空集合。
        """
        capabilities: set[str] = set()
        for plugin in self._plugins:
            try:
                capabilities |= set(plugin.stream_capabilities(stream_id))
            except Exception as exc:
                logger.error(
                    '工具插件查询会话能力失败，已跳过该插件本次贡献',
                    plugin=plugin.manifest.plugin_id,
                    error=str(exc),
                )
        return frozenset(capabilities)

    def _discover_root(self, root: Path) -> None:
        """扫描单个根目录；目录不存在或无法列举时跳过，不当作错误。"""
        if not root.is_dir():
            logger.debug('插件根目录不存在，跳过', root=str(root))
            return
        try:
            directories = sorted(
                entry for entry in root.iterdir() if entry.is_dir()
            )
        except OSError as exc:
            logger.error('插件根目录无法列举，已跳过', root=str(root), error=str(exc))
            return
        for directory in directories:
            self._discover_one(directory)

    def _discover_one(self, directory: Path) -> None:
        """发现并加载单个插件目录；任何失败都只影响该插件自身。"""
        try:
            manifest = load_manifest(directory / MANIFEST_FILENAME)
        except Exception as exc:
            # 清单错误的确切原因由异常消息携带，日志原样透出。
            logger.error(
                '插件清单加载失败，已跳过该插件',
                directory=str(directory),
                error=str(exc),
            )
            return
        if manifest.plugin_type != 'tool':
            # 适配器互斥且跑在独立进程，由进程入口按名字加载，刻意不并入扫目录
            # 这条路径；出现在这里属于放错了位置，记 warning 让人看到。
            logger.warning(
                '插件根目录下发现非工具插件，已忽略',
                directory=str(directory),
                plugin=manifest.plugin_id,
                plugin_type=manifest.plugin_type,
            )
            return
        origin = self._origins.get(manifest.plugin_id)
        if origin is not None:
            # 先扫描到的生效：根目录按优先级排序，内置在前，因此冲突时内置优先。
            # 后者的入口模块根本不会被执行。
            logger.warning(
                '插件 id 冲突，先扫描到的生效，后者被忽略',
                plugin=manifest.plugin_id,
                kept=str(origin),
                ignored=str(directory),
            )
            return
        try:
            plugin = load_tool_plugin(directory, manifest)
        except Exception as exc:
            logger.error(
                '工具插件加载失败，已跳过该插件',
                directory=str(directory),
                plugin=manifest.plugin_id,
                error=str(exc),
            )
            return
        self._origins[manifest.plugin_id] = directory
        self._plugins.append(plugin)
        logger.info(
            '工具插件已加载',
            plugin=manifest.plugin_id,
            name=manifest.name,
            version=manifest.version,
        )
