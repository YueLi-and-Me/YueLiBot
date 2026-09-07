"""锁死宿主入口在生产装配路径上确实注入了插件。

这条回归对应一次真机故障：链接读取插件被调用时抛「插件 yueli.link-reader 的宿主
入口尚未注入」，而当时全量用例是绿的。

缺口出在合流：契约层交付了 ``Plugin.bind_context``，组件层交付了
``PluginRegistry(context_factory=...)``，**中间那段——聊天服务把工厂传进去——两条线
都以为归对方**，于是 ``PluginRegistry()`` 一直是无工厂构造。三条线各自的用例都注入
桩件工厂，因此谁都没看见；漏传不会报错，只在插件真正读 ``self.ctx`` 的那一刻才炸。

所以这条用例只做一件事：**按生产路径构造 ChatService，断言每个被加载的插件都能真的
用上 ctx**。不许注入任何桩件——注入桩件就等于把要锁的那个缺陷重新藏起来。

依赖 ``src.core.services.chat`` 与随程序分发的内置插件。
"""

from __future__ import annotations

import sqlite3

from src.core.config.schema import Config
from src.core.services.chat import ChatService


def _noop(*_args: object, **_kwargs: object) -> None:
    """事件推送替身：本用例不关心事件。"""


def test_every_loaded_plugin_has_host_context(db: sqlite3.Connection) -> None:
    """生产装配下，注册表里每个插件都拿到了可用的宿主入口。"""
    chat = ChatService(db, None, None, None, _noop, cfg=Config())

    plugins = chat._plugins.tool_plugins()
    assert plugins, '随程序分发的内置插件至少应加载一个，否则这条用例什么都没验证'
    for plugin in plugins:
        # 访问而不是 hasattr：未注入时属性访问本身抛异常，hasattr 会把它吞掉。
        context = plugin.ctx
        assert context.host.bot_name == Config().bot.name
        assert context.paths.plugin_dir.name != '', (
            f'{plugin.manifest.plugin_id} 的 plugin_dir 未注入'
        )
        assert context.logger is not None


def test_context_carries_the_plugin_own_directory(db: sqlite3.Connection) -> None:
    """每个插件拿到的是自己的目录，不是共用一个。

    工厂签名带插件目录就是为了这件事：``config.toml`` 在各自目录下，共用一个
    目录会让插件读到别人的配置，而那种错在现场表现为「配置改了不生效」。
    """
    chat = ChatService(db, None, None, None, _noop, cfg=Config())

    directories = {
        plugin.manifest.plugin_id: plugin.ctx.paths.plugin_dir
        for plugin in chat._plugins.tool_plugins()
    }
    assert len(set(directories.values())) == len(directories), (
        f'插件目录出现重复：{directories}'
    )
    for plugin_id, directory in directories.items():
        assert (directory / '_manifest.json').is_file(), (
            f'{plugin_id} 的 plugin_dir 指向了没有清单的目录：{directory}'
        )
