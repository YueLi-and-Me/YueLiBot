"""插件自带配置的声明基类、生成与读取。

插件把自己的配置写成一个 Pydantic 模型（:class:`PluginConfig` 的子类），宿主据此
在插件目录里生成一份带注释的 ``config.toml``。声明只有模型这一份——不需要插件再
额外写一份 schema 或手写初始 TOML，两份声明必然随演进漂移。

::

    <插件目录>/config.toml

    [plugin]
    # 是否启用本插件；关闭后宿主不会加载它
    enabled = true

**读取分两阶段，这是刻意的：**

1. :func:`read_enabled_flag` 只读文件、**不导入插件代码**。已经生成过配置的插件
   走这一步就能判断该不该加载，因此一个入口代码写坏了的插件可以靠把 ``enabled``
   改成 ``false`` 彻底绕开，不必删目录。
2. :func:`ensure_plugin_config` 在文件尚不存在时按模型默认值生成它，这一步需要
   插件类，因而必须先导入。首次安装走这条路。

代价是「首次发现」必然会执行一次插件的入口模块顶层代码。没有别的办法——声明写在
代码里，不导入就读不到。第二次启动之后就走不到这条路了。

依赖 ``pydantic``；被 ``registry`` 在发现阶段调用，被具体插件继承。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import tomllib

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.core.logging.logger import get_logger

logger = get_logger(__name__)

# 插件目录下的配置文件名，与适配器插件的约定一致。
PLUGIN_CONFIG_FILENAME = 'config.toml'
# 承载插件自身配置的段名。插件的字段全部平铺在这一段里。
PLUGIN_SECTION = 'plugin'
# 启用开关的键名。read_enabled_flag 只认这一个键，其余字段留给插件自己解析。
ENABLED_KEY = 'enabled'


class PluginConfig(BaseModel):
    """插件配置的基类，至少带一个启用开关。

    插件按需继承并追加自己的字段，每个字段都应给出 ``description``——那句说明会
    原样成为生成出的 TOML 里的注释，是用户唯一能看到的字段解释。
    """

    model_config = ConfigDict(extra='forbid')

    enabled: bool = Field(
        default=True,
        description='是否启用本插件；false 时宿主不会加载它',
    )


def plugin_config_path(directory: Path) -> Path:
    """给出插件配置文件路径。

    :param directory: 插件目录。
    :return: ``<插件目录>/config.toml``；文件是否存在不在此校验。
    """
    return directory / PLUGIN_CONFIG_FILENAME


def read_enabled_flag(directory: Path) -> bool | None:
    """在不导入插件代码的前提下读出启用开关。

    :param directory: 插件目录。
    :return: 写了布尔字面量时返回它；文件不存在返回 ``None``（表示「还没生成过，
        交给 :func:`ensure_plugin_config` 处理」）；其余情况返回 ``True``。
    副作用：读取一次磁盘文件；不抛异常。

    **本函数是保守的快路径，不是判据本身。** 它只在看到明确的 ``false`` 时才敢
    短路掉导入；除此之外一律返回 ``True``，把结论让给 :func:`ensure_plugin_config`
    的完整校验——那一步会走 Pydantic，因而 ``enabled = "no"`` 这类可强制转换的写法
    仍然会被正确地判成关闭，只是多付一次导入的代价。

    两边不必写成同一套判据：在这里抄一份 Pydantic 的布尔强转表，就是第二份真相源。
    """
    path = plugin_config_path(directory)
    if not path.is_file():
        return None
    try:
        document = tomllib.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        logger.warning('插件配置读取失败，按启用处理', path=str(path), error=str(exc))
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


def ensure_plugin_config(
    directory: Path,
    model: type[PluginConfig],
) -> PluginConfig:
    """读取插件配置；文件不存在时按模型默认值生成一份带注释的。

    :param directory: 插件目录。
    :param model: 该插件声明的配置模型。
    :return: 已校验的配置实例。任何读取或校验失败都退回模型默认值，不抛异常——
        一个插件的配置写错不该让宿主的发现流程中断。
    副作用：文件不存在时写入一份新的 ``config.toml``；写入失败只记 warning，
        本次以默认值运行（只读安装或权限不足时会走到这里）。
    """
    path = plugin_config_path(directory)
    if not path.is_file():
        defaults = model()
        try:
            path.write_text(render_plugin_config(model), encoding='utf-8')
            logger.info('已生成插件配置', path=str(path))
        except OSError as exc:
            logger.warning('插件配置写入失败，本次按默认值运行', path=str(path), error=str(exc))
        return defaults
    try:
        document = tomllib.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        logger.warning('插件配置读取失败，按默认值运行', path=str(path), error=str(exc))
        return model()
    section = document.get(PLUGIN_SECTION)
    if not isinstance(section, dict):
        logger.warning(f'插件配置缺少 [{PLUGIN_SECTION}] 段，按默认值运行', path=str(path))
        return model()
    try:
        return model.model_validate(section)
    except ValidationError as exc:
        logger.warning('插件配置校验失败，按默认值运行', path=str(path), error=str(exc))
        return model()


def render_plugin_config(model: type[PluginConfig]) -> str:
    """把配置模型渲染成带注释的 TOML 文本。

    :param model: 配置模型类。
    :return: 完整文件内容，字段顺序与模型声明一致。
    :raises TypeError: 某个字段的默认值不是 TOML 能表达的类型。

    每个字段的 ``description`` 成为它上方的注释行。没有 ``description`` 的字段
    只写值——不编一句注释，凑出来的说明比没有更误导人。
    """
    lines = [
        '# 本文件由宿主按插件声明的配置模型自动生成，可以直接编辑。',
        '# 删掉它会在下次启动时重新生成一份默认配置。',
        '',
        f'[{PLUGIN_SECTION}]',
    ]
    for name, field in model.model_fields.items():
        description = (field.description or '').strip()
        if description:
            lines.append(f'# {description}')
        lines.append(f'{name} = {_toml_value(field.get_default(call_default_factory=True))}')
    return '\n'.join(lines) + '\n'


def _toml_value(value: Any) -> str:
    """把一个默认值渲染成 TOML 字面量。

    :param value: 字段默认值。
    :return: TOML 字面量文本。
    :raises TypeError: 类型不受支持——不静默降级成字符串，那会让生成出的配置
        读回来是另一个类型，且到运行期才发现。
    """
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_toml_value(item) for item in value) + ']'
    if isinstance(value, dict):
        pairs = ', '.join(f'{key} = {_toml_value(item)}' for key, item in value.items())
        return '{' + pairs + '}'
    raise TypeError(f'插件配置字段的默认值无法渲染成 TOML：{value!r}')


__all__ = [
    'ENABLED_KEY',
    'PLUGIN_CONFIG_FILENAME',
    'PLUGIN_SECTION',
    'PluginConfig',
    'ensure_plugin_config',
    'plugin_config_path',
    'read_enabled_flag',
    'render_plugin_config',
]
