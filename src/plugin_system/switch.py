"""读取插件目录自带的启用开关。

开关放在插件自己的 ``config.toml`` 里而不是主体配置里：开关随插件一起装、一起删，
主配置里不会留下指向已删插件的孤儿项，用户也不必为了开一个插件去改另一个文件。

::

    <插件目录>/config.toml

    [plugin]
    enabled = true

**文件不存在时视为启用。** 这是刻意的向后兼容：先于本机制存在的插件都没有这份
配置，要求它们补一个文件才肯加载，等于升级即失能，而能力消失只在模型「不会用某个
工具」时才被察觉，极难归因。插件想默认关闭就显式写 ``enabled = false``。

本模块只回答「这个目录该不该加载」，不解析插件自己的其余配置项——那属于插件的
``on_load``，本模块不替它定型字段。被 ``registry`` 在发现阶段调用。
"""

from __future__ import annotations

from pathlib import Path

import tomllib

from src.core.logging.logger import get_logger

logger = get_logger(__name__)

# 插件目录下的可选配置文件名，与适配器插件的约定一致。
PLUGIN_CONFIG_FILENAME = 'config.toml'
# 承载开关的段名与键名。
PLUGIN_SECTION = 'plugin'
ENABLED_KEY = 'enabled'


def plugin_enabled(directory: Path) -> bool:
    """判断一个插件目录是否处于启用状态。

    :param directory: 插件目录，可能含 ``config.toml``。
    :return: 启用为 ``True``。配置文件不存在、段或键缺失都按启用处理；
        只有显式写成 ``false`` 才关闭。
    副作用：读取一次磁盘文件；不抛异常。

    读取失败（文件损坏、无权限、``enabled`` 不是布尔值）一律记 warning 并按**启用**
    处理：
    - 现象：一份写坏的 config.toml 会让插件照常加载，而不是被静默关掉。
    - 原因：关闭是显式意图，坏文件表达不了意图；把「读不懂」当成「要关掉」，
      等于让一个笔误悄悄拿掉一项能力。
    - 后果：坏文件的反馈是启动日志里的 warning，不是功能凭空消失。
    """
    path = directory / PLUGIN_CONFIG_FILENAME
    if not path.is_file():
        return True
    try:
        document = tomllib.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            '插件配置读取失败，按启用处理',
            path=str(path),
            error=str(exc),
        )
        return True
    section = document.get(PLUGIN_SECTION)
    if not isinstance(section, dict) or ENABLED_KEY not in section:
        return True
    value = section[ENABLED_KEY]
    if not isinstance(value, bool):
        logger.warning(
            f'插件配置的 [{PLUGIN_SECTION}] {ENABLED_KEY} 不是布尔值，按启用处理',
            path=str(path),
            value=repr(value),
        )
        return True
    return value


__all__ = [
    'ENABLED_KEY',
    'PLUGIN_CONFIG_FILENAME',
    'PLUGIN_SECTION',
    'plugin_enabled',
]
