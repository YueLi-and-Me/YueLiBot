"""按目录发现并实例化插件。

插件目录名允许含连字符（``yueli-napcat-adapter``），不是合法的 Python 包名，因此
按文件路径加载而不是按包导入。加载约定只有一条：``plugin.py`` 里恰好定义一个期望
基类的子类——零个说明忘了写，多个说明入口有歧义，两者都当场报错，不猜测该用哪一个。

``_load_module`` 与 ``_single_plugin_class`` 是类型无关的通用逻辑，适配器与工具
两条加载路径共用同一份，只是期望的基类不同：``load_adapter_plugin`` 被适配器进程
入口按名字调用，``load_tool_plugin`` 被插件注册表在扫目录发现时调用。

依赖 ``manifest``、``adapter`` 与 ``tools``；被适配器进程入口与插件注册表调用，
不被主体业务代码引用。
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import List, Type, TypeVar

import importlib.util
import inspect
import sys

from .adapter import AdapterPlugin
from .manifest import AdapterManifest, PluginManifest, load_manifest
from .plugin import Plugin
from .tools import ToolPlugin


# 插件目录里两个固定文件名。清单描述插件是什么，入口模块提供实现。
MANIFEST_FILENAME = '_manifest.json'
PLUGIN_FILENAME = 'plugin.py'

# 唯一实现判定按期望基类收窄；类型参数让两条加载路径各自拿到准确的返回类型。
PluginT = TypeVar('PluginT', bound=Plugin)


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
    spec = importlib.util.spec_from_file_location(
        module_name, path, submodule_search_locations=[str(path.parent)],
    )
    if spec is None or spec.loader is None:
        raise PluginLoadError(f'无法为插件入口构造模块规格：{path}')
    module = importlib.util.module_from_spec(spec)
    # 相对导入需要可查到的父包；不先登记会报父包不存在，兄弟模块无法加载。
    # 入口自身作为包根，避免再执行目录 __init__.py 造成入口有两个模块身份。
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # 执行中断后留下入口或兄弟模块，会让重试复用半执行状态，表现为改代码不生效。
        # 清理本次包命名空间后原样上抛，确保下一次加载重新执行入口及其相对导入。
        for name in list(sys.modules):
            if name == module_name or name.startswith(module_name + '.'):
                del sys.modules[name]
        raise
    return module


def _single_plugin_class(
    module: ModuleType,
    path: Path,
    base_class: Type[PluginT],
) -> Type[PluginT]:
    """在入口模块里找出唯一实现了期望基类的插件类。

    只认在该模块内定义的类：基类本身是被导入进来的，把它算进候选会让每个模块都
    至少有两个候选。

    :param module: 已加载的入口模块。
    :param path: 入口路径，仅用于错误信息定位。
    :param base_class: 期望的插件基类；候选必须是它的子类，入口里其它类型的插件
        实现（例如与工具插件同处一个模块的适配器类）不计入候选。
    :return: 唯一的插件类。
    :raises PluginLoadError: 候选数量不是一个。
    """
    candidates: List[Type[PluginT]] = [
        member
        for _name, member in inspect.getmembers(module, inspect.isclass)
        if issubclass(member, base_class)
        and member is not base_class
        and member.__module__ == module.__name__
    ]
    if not candidates:
        raise PluginLoadError(f'插件入口没有定义 {base_class.__name__} 子类：{path}')
    if len(candidates) > 1:
        names = '、'.join(sorted(cls.__name__ for cls in candidates))
        raise PluginLoadError(
            f'插件入口定义了多个 {base_class.__name__} 子类，入口有歧义：{names}'
        )
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
    manifest = load_manifest(directory / MANIFEST_FILENAME)
    if not isinstance(manifest, AdapterManifest):
        raise PluginLoadError(
            f'{directory} 的清单类型是 {manifest.plugin_type}，不是适配器'
        )
    entry = directory / PLUGIN_FILENAME
    # 模块名用插件标识派生：两个适配器的入口文件同名，按文件名注册会互相覆盖。
    module = _load_module(entry, manifest.plugin_id.replace('.', '_'))
    plugin_class = _single_plugin_class(module, entry, AdapterPlugin)
    return plugin_class(manifest, **options)


def load_tool_plugin(directory: Path, manifest: PluginManifest) -> ToolPlugin:
    """用已解析的清单加载一个工具插件目录，返回可用的插件实例。

    清单由调用方（注册表）先行解析：扫目录发现时必须先拿到插件类型与标识做分派
    与去重——类型不符的目录不该走到加载，同 id 的后出现者更不该执行其入口模块。

    :param directory: 插件目录，需包含 ``plugin.py``。
    :param manifest: 该目录已校验的清单，``plugin_type`` 必须为 ``tool``。
    :return: 已用清单构造、尚未 ``on_load`` 的工具插件实例。
    :raises PluginLoadError: 清单类型不符、入口缺失或实现不唯一。
    :raises TypeError: 插件构造函数拒绝清单以外的参数。
    副作用：读取入口文件并执行其顶层代码。
    """
    if manifest.plugin_type != 'tool':
        raise PluginLoadError(
            f'{directory} 的清单类型是 {manifest.plugin_type}，不是工具插件'
        )
    entry = directory / PLUGIN_FILENAME
    module = _load_module(entry, manifest.plugin_id.replace('.', '_'))
    plugin_class = _single_plugin_class(module, entry, ToolPlugin)
    return plugin_class(manifest)
