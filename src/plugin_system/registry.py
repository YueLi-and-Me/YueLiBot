"""扫描插件根目录、按类型分派，并把插件聚合成主体可查询、可分发的集合。

目录约定::

    src/plugins/built_in/<插件名>/     内置插件，随程序发布
    plugins/<插件名>/                  第三方插件，用户自行放置

每个插件目录含 ``_manifest.json`` 与 ``plugin.py``，与适配器一致；可选的
``config.toml`` 携带该插件自己的启用开关与配置项，见 ``switch`` 模块。两个根目录都扫，
调用方把内置根目录排在前：同 id 冲突时先扫描到的生效，后者被忽略并记 warning。

注册表是主体与工具插件之间的唯一界面：主体只调 ``discover`` / ``load_all`` /
``unload_all`` / ``rewrite_inbound`` / ``observe_inbound`` /
``stream_capabilities`` / ``register_commands``，不直接接触单个插件。隔离原则
贯穿全表：第三方插件目录里混着一个坏插件时，整个 Bot 起不来是不可接受的，
因此发现、加载、入站改写、入站观察、能力查询任何一步的单组件失败都只影响该
组件自身。命令注册是例外：重名命令会让调用当场抛错而不是跳过，因为静默丢掉
一条命令与「命令怎么没出现」的排障成本远高于启动失败一次。

依赖 ``loader``、``tools`` 与 ``context``；被主体（聊天服务）在启动期驱动。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.core.commands.registry import register_command
from src.core.logging.logger import get_logger
from src.core.platform_io.types import InboundMessage

from .context import PluginContext
from .loader import MANIFEST_FILENAME, load_tool_plugin
from .config import ensure_plugin_config, read_enabled_flag
from .manifest import load_manifest
from .tools import ToolPlugin


logger = get_logger(__name__)


class PluginRegistry:
    """工具插件的注册表：发现、生命周期驱动与聚合分发。

    生命周期固定为 ``discover`` → ``load_all`` → 运行期聚合调用 → ``unload_all``，
    由主体按序驱动。``load_all`` 失败的插件会从注册表移除，此后的聚合调用只覆盖
    加载成功的插件。
    """

    def __init__(
        self,
        context_factory: Optional[Callable[[str, Path], PluginContext]] = None,
    ) -> None:
        """初始化空注册表。

        :param context_factory: 按插件 id 与插件目录构造宿主入口的工厂；传入时每个已启用
            插件在 ``bind_config`` 之后、``on_load`` 之前得到自己的入口实例
            （日志器绑定插件 id，因此必须每插件一个）。缺省 ``None`` 表示本表
            不注入入口，供宿主接线尚未就位的装配路径使用。
        """
        self._plugins: List[ToolPlugin] = []
        # 插件 id → 来源目录，用于同 id 冲突时指出被保留者与后来者各自的位置。
        self._origins: Dict[str, Path] = {}
        # 已成功 on_load 的插件；unload_all 只卸载它们，据此保证自身幂等。
        self._loaded: List[ToolPlugin] = []
        self._context_factory = context_factory

    def discover(self, roots: Sequence[Path]) -> None:
        """按序扫描插件根目录，加载全部已启用的合法工具插件。

        插件的启用开关在它自己的目录里（``config.toml`` 的 ``[plugin] enabled``），
        不在主体配置里——开关随插件一起装、一起删，主配置里不会留下指向已删插件
        的孤儿项，用户也不必为了开一个插件去改另一个文件。

        :param roots: 插件根目录，按优先级从高到低排列——同 id 冲突时先扫描到的
            生效，后者被忽略并记 warning；内置根目录因此应排在第三方之前。不存在
            的根目录直接跳过（用户可能从未放置第三方插件）。
        :return: ``None``。
        副作用：读取各插件目录的清单、开关与入口文件，执行入口模块顶层代码；单个
            插件的任何失败只记 error 并跳过，不中断其余插件的发现。
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

    async def rewrite_inbound(self, inbound: InboundMessage, text: str) -> str:
        """串行驱动全部入站改写器，返回落库前应使用的最终正文。

        改写器按 ``(order, 插件 id)`` 升序接力：后一个收到的是前一个改写后的
        正文（以替换 ``text`` 字段的方式传递）。顺序必须确定——顺序不确定意味着
        同一条消息两次运行得到不同结果，那类问题无法复现也就无法修。单个改写器
        抛异常、返回非字符串或返回空白正文时记 error、保留上一步正文并继续
        下一个：入站是主链路，插件的 bug 既不该让消息进不来，也不该把消息变没。

        组件集合随插件的增删变化（``load_all`` 会移除加载失败的插件），聚合不缓存、
        每次分发重算。

        :param inbound: 原始入站消息；其 ``text`` 不被本方法读取，正文以 ``text``
            参数为准（调用方已完成首尾空白规整）。
        :param text: 当前正文，作为第一个改写器的输入。
        :return: 最终正文；没有任何改写器时原样返回 ``text``。
        """
        entries: List[Tuple[int, str, Any]] = []
        for plugin in self._plugins:
            for spec, handler in plugin.inbound_rewrites():
                entries.append((spec.order, plugin.manifest.plugin_id, handler))
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        for _order, plugin_id, handler in entries:
            try:
                result = await handler(replace(inbound, text=text))
            except Exception as exc:
                logger.error(
                    '插件改写入站正文失败，保留上一步正文',
                    plugin=plugin_id,
                    error=str(exc),
                )
                continue
            if result is None:
                continue
            if not isinstance(result, str):
                logger.error(
                    '插件改写器返回了非字符串结果，保留上一步正文',
                    plugin=plugin_id,
                    result_type=type(result).__name__,
                )
                continue
            if not result.strip():
                logger.error(
                    '插件改写器把正文改成了空白，保留上一步正文',
                    plugin=plugin_id,
                )
                continue
            text = result
        return text

    def observe_inbound(self, stream_id: int, message_id: int, inbound: Any) -> None:
        """把一条已入库的入站消息分发给全部观察组件。

        入站是主链路：单个观察器抛异常时记 error 并继续下一个，插件的 bug 不该让
        消息进不来。

        :param stream_id: 会话编号。
        :param message_id: 该消息在主体侧的内部编号。
        :param inbound: 入站消息对象；字段见 ``src.core.platform_io.types``。
        :return: ``None``。
        """
        for plugin in self._plugins:
            for handler in plugin.inbound_observers():
                try:
                    handler(stream_id, message_id, inbound)
                except Exception as exc:
                    logger.error(
                        '插件观察入站消息失败，已跳过该观察器本次调用',
                        plugin=plugin.manifest.plugin_id,
                        error=str(exc),
                    )

    def register_commands(self) -> None:
        """把全部插件的命令组件注册进开发者命令目录。

        在发现之后由宿主一次性调用。已关闭的插件不会被发现，其命令因此不进
        目录。重名与非法声明由 ``register_command`` 当场抛错，调用方不捕获：
        静默跳过会让「命令怎么没出现」无从排障，而启动失败一次就把确切原因
        给出来了。

        :return: ``None``。
        :raises ValueError: 命令名已注册（含与内置命令重名）或声明非法。
        :raises re.error: 正则模式无法编译。
        副作用：向进程内命令目录追加条目；重复调用同一批插件会因重名抛错。
        """
        for plugin in self._plugins:
            for declaration, handler in plugin.commands():
                register_command(
                    declaration.name,
                    declaration.pattern,
                    declaration.description,
                )(handler)

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

    def _log_disabled(self, plugin_id: str, directory: Path) -> None:
        """记录一条「已被关闭」。

        记 info 而不是静默跳过：配置里关掉的东西必须在控制台看得见，否则
        「工具怎么没出现」只能靠翻配置猜。
        """
        logger.info(
            '工具插件已在自身配置中关闭，跳过加载',
            plugin=plugin_id,
            directory=str(directory),
        )

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
        # 第一阶段：只读文件，不导入插件代码。已生成过配置的插件在这里就能被挡下，
        # 因此入口写坏了的插件可以靠把 enabled 改成 false 彻底绕开，不必删目录。
        if read_enabled_flag(directory) is False:
            self._log_disabled(manifest.plugin_id, directory)
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
        # 第二阶段：配置文件尚不存在时按插件声明的模型生成它。这一步要拿到插件类，
        # 所以必须在导入之后——首次安装因此会执行一次入口模块，之后走不到这里。
        try:
            config = ensure_plugin_config(directory, type(plugin).config_model)
        except Exception as exc:
            logger.error(
                '工具插件配置处理失败，已跳过该插件',
                directory=str(directory),
                plugin=manifest.plugin_id,
                error=str(exc),
            )
            return
        if not config.enabled:
            self._log_disabled(manifest.plugin_id, directory)
            return
        plugin.bind_config(config)
        if self._context_factory is not None:
            # 时序固定：bind_config 之后、on_load 之前。入口构造失败属于宿主接线
            # 问题而不是插件缺陷，让它当场抛出，不按单插件失败隔离。
            plugin.bind_context(self._context_factory(manifest.plugin_id, directory))
        self._origins[manifest.plugin_id] = directory
        self._plugins.append(plugin)
        logger.info(
            '工具插件已加载',
            plugin=manifest.plugin_id,
            name=manifest.name,
            version=manifest.version,
        )
