"""按目录发现并实例化适配器插件。

适配器目录名含连字符（``yueli-napcat-adapter``），不是合法的 Python 包名，因此
按文件路径加载而不是按包导入。加载约定只有一条：``plugin.py`` 里恰好定义一个
:class:`AdapterPlugin` 子类——零个说明忘了写，多个说明入口有歧义，两者都当场报错，
不猜测该用哪一个。

依赖 ``manifest`` 与 ``adapter``；被适配器进程入口调用，不被主体业务代码引用。
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import List, Type

import importlib.util
import inspect

from .adapter import AdapterPlugin
from .manifest import AdapterManifest, load_manifest


# 适配器目录里两个固定文件名。清单描述插件是什么，入口模块提供实现。
MANIFEST_FILENAME = '_manifest.json'
PLUGIN_FILENAME = 'plugin.py'


class PluginLoadError(RuntimeError):
    """插件目录结构不合约定，或入口模块无法确定唯一实现。"""


def _load_module(path: Path, module_name: str) -> ModuleType:
    """按文件路径加载入口模块。

    :param path: ``plugin.py`` 的路径。
    :param module_name: 注册到 ``sys.modules`` 的名字，用插件标识派生以避免同名覆盖。
    :return: 已执行的模块对象。
    :raises PluginLoadError: 文件不存在或无法构造模块规格。
    副作用：执行模块顶层代码。
    """
    if not path.is_file():
        raise PluginLoadError(f'插件入口不存在：{path}')
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f'无法为插件入口构造模块规格：{path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _single_plugin_class(module: ModuleType, path: Path) -> Type[AdapterPlugin]:
    """在入口模块里找出唯一的适配器插件实现。

    只认在该模块内定义的类：基类本身是被导入进来的，把它算进候选会让每个模块都
    至少有两个候选。

    :param module: 已加载的入口模块。
    :param path: 入口路径，仅用于错误信息定位。
    :return: 唯一的插件类。
    :raises PluginLoadError: 候选数量不是一个。
    """
    candidates: List[Type[AdapterPlugin]] = [
        member
        for _name, member in inspect.getmembers(module, inspect.isclass)
        if issubclass(member, AdapterPlugin)
        and member is not AdapterPlugin
        and member.__module__ == module.__name__
    ]
    if not candidates:
        raise PluginLoadError(f'插件入口没有定义 AdapterPlugin 子类：{path}')
    if len(candidates) > 1:
        names = '、'.join(sorted(cls.__name__ for cls in candidates))
        raise PluginLoadError(f'插件入口定义了多个 AdapterPlugin 子类，入口有歧义：{names}')
    return candidates[0]


def load_adapter_plugin(directory: Path, **options: object) -> AdapterPlugin:
    """加载一个适配器目录，返回可用的插件实例。

    :param directory: 适配器目录，需包含 ``_manifest.json`` 与 ``plugin.py``。
    :param options: 透传给插件构造函数的具名参数。宿主用它注入随部署变化的路径，
        目前只有 ``runtime_path``——主体运行时信息的位置随数据目录配置变化，
        插件的默认值只在数据目录取默认位置时成立。
    :return: 已用清单构造、尚未 ``on_load`` 的插件实例。
    :raises PluginLoadError: 目录不存在、入口缺失或实现不唯一。
    :raises ManifestError: 清单结构非法。
    :raises CapabilityError: 清单声明了非法能力标识。
    :raises TypeError: 插件构造函数不接受宿主注入的具名参数。
    副作用：读取两个文件并执行入口模块顶层代码。
    """
    if not directory.is_dir():
        raise PluginLoadError(f'适配器目录不存在：{directory}')
    manifest: AdapterManifest = load_manifest(directory / MANIFEST_FILENAME)
    entry = directory / PLUGIN_FILENAME
    # 模块名用插件标识派生：两个适配器的入口文件同名，按文件名注册会互相覆盖。
    module = _load_module(entry, manifest.plugin_id.replace('.', '_'))
    plugin_class = _single_plugin_class(module, entry)
    return plugin_class(manifest, **options)
