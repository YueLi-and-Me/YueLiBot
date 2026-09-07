"""定义工具插件基类与四类组件的收集方法。

基类只承载生命周期与挂载点：组件声明（``@tool`` / ``@inbound_rewrite`` /
``@inbound_observe`` / ``@command``）写在 ``components`` 里，插件作者把声明直接
标在方法上，本模块的收集方法按属性标记把它们找出来。声明与基类分开，新增一类
组件时基类不必再长出一个可覆写方法；同一个插件也因此可以声明多个同类组件。

收集方法扫描的是实例而非类，因此拿到的是绑定方法，执行时 ``self`` 已就位。

依赖 ``plugin`` 基类、``components`` 的声明标记与 ``src.core.tooling`` 的执行
协议；被具体工具插件继承，被宿主收集后按组件类型分别取用。
"""

from __future__ import annotations

from typing import Any, FrozenSet, List, Optional, Tuple

import inspect

from src.core.tooling.spec import ToolSpec

from .components import (
    CommandDeclaration,
    InboundObserveHandler,
    InboundRewriteHandler,
    InboundRewriteSpec,
    PluginCommandHandler,
    _BoundToolExecutor,
    _COMMAND_ATTR,
    _INBOUND_OBSERVE_ATTR,
    _INBOUND_REWRITE_ATTR,
    _TOOL_SPEC_ATTR,
)
from .context import PluginContext
from .manifest import PluginManifest
from .plugin import Plugin


class ToolPlugin(Plugin):
    """贡献外部工具的插件基类。

    四类组件经装饰器声明、由本类的收集方法聚出：工具进 ToolRegistry，命令进
    开发者命令目录，两类入站组件由聊天服务在落库前后两个时机分发。
    ``stream_capabilities`` 不是组件：它按会话贡献能力名，决定组件在会话内的
    可见性，没有独立的执行体。
    """

    def __init__(self, manifest: PluginManifest) -> None:
        """保存清单并置空宿主入口。

        :param manifest: 已校验的插件清单。
        """
        super().__init__(manifest)
        # 宿主入口由 bind_context 注入；构造期访问不到，置 None 仅为让属性存在。
        self._context: Optional[PluginContext] = None

    def _bound_methods(self) -> List[Tuple[str, Any]]:
        """按属性名字典序枚举实例上的绑定方法。

        - 现象：宿主入口 ``ctx`` 这类「未注入就报错」的属性会让
          ``inspect.getmembers`` 的逐属性取值直接抛出，四类组件全收集不到。
        - 原因：``getmembers`` 只消化 ``AttributeError``，而带早失败语义的属性
          抛的是 ``RuntimeError``。
        - 后果：枚举必须显式逐属性 ``getattr`` 并跳过取值抛错者——组件收集只
          关心方法上的声明标记，任何一个属性的评价失败都不该打断它。

        :return: ``(属性名, 绑定方法)`` 列表，按属性名排序。
        """
        members: List[Tuple[str, Any]] = []
        for name in dir(self):
            try:
                member = getattr(self, name)
            except Exception:
                continue
            if inspect.ismethod(member):
                members.append((name, member))
        return members

    def tools(self) -> List[Tuple[ToolSpec, _BoundToolExecutor]]:
        """收集本插件用 ``@tool`` 声明的全部工具。

        子类通常不需要覆写；确有动态生成工具的需要时可以覆写并自行返回。

        **本方法在 ``on_load`` 之前被调用。** 宿主必须先把工具登记进注册表，
        才能把注册表交给对话代理构造，而 ``on_load`` 允许做 I/O、只能在事件循环里
        执行，两者的先后由此固定。因此工具声明不得依赖 ``on_load`` 建立的状态——
        需要按配置决定声明什么的工具，当前这套装配顺序还支持不了。

        :return: ``(声明, 执行器)`` 列表，按工具名排序保证登记顺序可复现。
        :raises ValueError: 同一插件内两个方法声明了同名工具——跨插件重名由注册表
            拒绝，插件内重名在这里就该发现。
        """
        collected: dict[str, Tuple[ToolSpec, _BoundToolExecutor]] = {}
        for _name, member in self._bound_methods():
            spec = getattr(member.__func__, _TOOL_SPEC_ATTR, None)
            if spec is None:
                continue
            if spec.name in collected:
                raise ValueError(
                    f'插件 {self.manifest.plugin_id} 内重复声明了工具 {spec.name}'
                )
            collected[spec.name] = (spec, _BoundToolExecutor(member))
        return [collected[key] for key in sorted(collected)]

    def inbound_rewrites(self) -> List[Tuple[InboundRewriteSpec, InboundRewriteHandler]]:
        """收集本插件用 ``@inbound_rewrite`` 声明的全部改写器。

        :return: ``(声明, 执行体)`` 列表，按 ``(order, 方法名)`` 升序——插件内
            的相对顺序必须确定，注册表再按 ``(order, 插件 id)`` 聚合跨插件顺序。
        """
        collected: List[Tuple[InboundRewriteSpec, InboundRewriteHandler]] = []
        for name, member in self._bound_methods():
            spec = getattr(member.__func__, _INBOUND_REWRITE_ATTR, None)
            if spec is not None:
                collected.append((spec, member))
        collected.sort(key=lambda item: (item[0].order, item[1].__name__))
        return collected

    def inbound_observers(self) -> List[InboundObserveHandler]:
        """收集本插件用 ``@inbound_observe`` 声明的全部观察器。

        :return: 执行体列表，按方法名升序；观察没有接力语义，顺序只为可复现。
        """
        return [
            member
            for _name, member in self._bound_methods()
            if getattr(member.__func__, _INBOUND_OBSERVE_ATTR, None) is not None
        ]

    def commands(self) -> List[Tuple[CommandDeclaration, PluginCommandHandler]]:
        """收集本插件用 ``@command`` 声明的全部命令。

        :return: ``(声明, 执行体)`` 列表，按命令名排序保证注册顺序可复现。
        :raises ValueError: 同一插件内两个方法声明了同名命令——跨插件与内置命令的
            重名由 ``register_command`` 在注册期拒绝，插件内重名在这里就该发现。
        """
        collected: dict[str, Tuple[CommandDeclaration, PluginCommandHandler]] = {}
        for _name, member in self._bound_methods():
            declaration = getattr(member.__func__, _COMMAND_ATTR, None)
            if declaration is None:
                continue
            if declaration.name in collected:
                raise ValueError(
                    f'插件 {self.manifest.plugin_id} 内重复声明了命令 {declaration.name}'
                )
            collected[declaration.name] = (declaration, member)
        return [collected[key] for key in sorted(collected)]

    def stream_capabilities(self, stream_id: int) -> FrozenSet[str]:
        """返回本插件为该会话贡献的平台能力。

        用于表达「这个工具在这个会话里有没有东西可读」——例如合并转发工具只在
        缓存里确实有转发内容的会话才该出现在工具声明里。

        :param stream_id: 会话编号。
        :return: 能力名集合；默认空集合，表示不贡献任何能力。
        """
        return frozenset()

    def bind_context(self, context: PluginContext) -> None:
        """由宿主在 ``bind_config`` 之后、``on_load`` 之前注入宿主入口。

        :param context: 宿主为本插件构造的 :class:`PluginContext`。
        :return: ``None``。
        副作用：保存入口对象；插件自身不应调用本方法。

        与 ``bind_config`` 同样不走构造函数：构造签名是插件契约的一部分，加载器
        统一以 ``plugin_class(manifest)`` 构造。时序固定在 ``on_load`` 之前，因此
        ``on_load`` 及之后的组件执行体经 ``self.ctx`` 访问宿主总是安全的；声明
        收集（``tools`` / ``commands`` 等）仍不得依赖它——收集发生在更早的构造期。
        """
        self._context = context

    @property
    def ctx(self) -> PluginContext:
        """返回宿主注入的 :class:`PluginContext`。

        :raises RuntimeError: 宿主尚未注入。未注入就访问多半是把宿主调用写进了
            声明收集期，当场报错比返回 ``None`` 让错误在远处炸掉更好定位。
        """
        if self._context is None:
            raise RuntimeError(
                f'插件 {self.manifest.plugin_id} 的宿主入口尚未注入，'
                '组件执行体只能在 on_load 及之后访问 self.ctx'
            )
        return self._context
