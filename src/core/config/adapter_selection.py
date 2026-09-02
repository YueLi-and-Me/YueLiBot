"""解析当前启用的 QQ 适配器，并给出它的连接配置位置与连接段名。

两个协议端后端互斥，同时只能启用一个。这个事实此前只存在于 Electron 监护器里
的一个常量，主体侧无从知晓，于是设置页只能按写死的文件名去读一份可能根本没人
用的配置——SnowLuma 上线后设置页写的 ``config/napcat.toml`` 就一直是死文件。
本模块把「当前启用哪个适配器」收敛成 ``config/adapter.toml`` 一处声明，
Electron 与主体各自读取同一份事实。

对外提供插件目录名、连接配置路径与清单声明的连接段名；清单解析复用
``src.plugin_system`` 的加载器，不在这里重复解析 ``_manifest.json``。被
``src.core.config.settings_webui`` 调用。
"""

from __future__ import annotations

from pathlib import Path

from src.plugin_system import AdapterManifest, load_manifest

import tomllib

# 声明文件名与其中的字段名；Electron 首次启动时按同名约定创建这份文件。
ADAPTER_SELECTION_FILENAME = 'adapter.toml'
ADAPTER_SELECTION_FIELD = 'plugin'
# 适配器的连接配置固定与插件同目录、同名，目录名即适配器身份。
ADAPTER_CONFIG_FILENAME = 'config.toml'
# 适配器插件根目录：由本文件位置推出仓库根，避免依赖进程工作目录。
ADAPTERS_ROOT = Path(__file__).resolve().parents[3] / 'adapters'


def read_active_adapter(config_dir: Path) -> str:
    """读取当前启用的适配器插件目录名。

    :param config_dir: 主体配置目录，声明文件位于其中。
    :return: ``adapters/`` 下的插件目录名。
    :raises OSError: 声明文件不存在或无法读取。
    :raises tomllib.TOMLDecodeError: 声明文件不是合法 TOML。
    :raises ValueError: 声明文件缺少非空的插件目录名。缺失不
        回退到某个默认适配器：猜错的那个适配器读的是另一份配置，表现为设置页
        改了参数却不生效，比直接报错难查得多。
    """
    path = config_dir / ADAPTER_SELECTION_FILENAME
    # 这份声明只有一个字段，不套 [inner].version：它不随配置结构演进，
    # 加一层版本壳只会多一处需要同步升级的地方。
    with open(path, 'rb') as file:
        document = tomllib.load(file)
    plugin = document.get(ADAPTER_SELECTION_FIELD)
    if not isinstance(plugin, str) or not plugin.strip():
        raise ValueError(
            f'{path} 缺少非空的 {ADAPTER_SELECTION_FIELD}，无法确定启用哪个适配器'
        )
    return plugin.strip()


def adapter_directory(plugin_dir: str) -> Path:
    """给出适配器插件目录的绝对路径。

    :param plugin_dir: ``adapters/`` 下的插件目录名。
    :return: 插件目录绝对路径。
    :raises ValueError: 目录不存在；声明指向一个装不上的适配器必须当场暴露。
    """
    directory = ADAPTERS_ROOT / plugin_dir
    if not directory.is_dir():
        raise ValueError(f'适配器目录不存在：{directory}')
    return directory


def adapter_config_path(plugin_dir: str) -> Path:
    """给出该适配器连接配置的绝对路径。

    :param plugin_dir: ``adapters/`` 下的插件目录名。
    :return: 插件目录下 ``config.toml`` 的绝对路径；文件是否存在不在此校验。
    :raises ValueError: 插件目录不存在。
    """
    return adapter_directory(plugin_dir) / ADAPTER_CONFIG_FILENAME


def adapter_config_section(plugin_dir: str) -> str:
    """读取该适配器在磁盘配置里使用的连接段名。

    :param plugin_dir: ``adapters/`` 下的插件目录名。
    :return: 清单中的 ``config_section``。
    :raises ValueError: 插件目录不存在，或清单不是适配器清单。
    :raises ManifestError: 清单结构非法。
    :raises OSError: 清单无法读取。
    """
    manifest = load_manifest(adapter_directory(plugin_dir) / '_manifest.json')
    if not isinstance(manifest, AdapterManifest):
        raise ValueError(f'{plugin_dir} 的清单不是适配器清单，没有连接段名')
    return manifest.config_section


__all__ = [
    'ADAPTERS_ROOT',
    'ADAPTER_CONFIG_FILENAME',
    'ADAPTER_SELECTION_FIELD',
    'ADAPTER_SELECTION_FILENAME',
    'adapter_config_path',
    'adapter_config_section',
    'adapter_directory',
    'read_active_adapter',
]
