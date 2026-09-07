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
2. :func:`ensure_plugin_config` 生成并对账配置：文件不存在时按模型默认值生成；
   已存在时补齐模型新增的字段、报告模型已不认识的残留键、逐字段校验用户写的值。
   这一步需要插件类，因而必须先导入。首次安装走这条路。

对账的处置口径沿用主配置 ``src.core.config.upgrade`` 文件头写的那套：新增字段
只追加不改写，废弃字段只报告不删除。

代价是「首次发现」必然会执行一次插件的入口模块顶层代码。没有别的办法——声明写在
代码里，不导入就读不到。第二次启动之后就走不到这条路了。

依赖 ``pydantic``；被 ``registry`` 在发现阶段调用，被具体插件继承。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import re
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
    """读取插件配置并与模型对账：补齐新增字段、报告退休键、逐字段校验。

    文件不存在时按模型默认值生成一份带注释的；已存在时做三件事——

    1. 模型里新增、文件里还没有的字段按默认值追加进 ``[plugin]`` 段，字段的
       ``description`` 原样落成注释；只追加，不改写已有的行、注释与顺序。
    2. 文件里残留、模型已不认识的键原样留在文件里，按 warning 报告——删除
       不可逆，由人决定去留（沿用主配置 ``src.core.config.upgrade`` 文件头
       的口径）。
    3. 校验按字段逐个处置：值非法的那个字段退回默认值并单独报告，其余用户
       填的值全部保留。曾经的做法是整份 ``model_validate`` 失败就退回全默认，
       实测一个残留键会把用户手填的代理与限额一起清零。

    写文件前不做备份，这是刻意的取舍：主配置备份是因为 ``config/`` 含明文密钥
    且不在版本控制里；插件的 ``config.toml`` 只有插件自己的开关与参数，且这里
    只做追加式写入，不会丢已有内容，多一层备份目录反而多一处要清理的东西。

    :param directory: 插件目录。日志里的 ``plugin`` 取目录名——清单 id 要读
        ``_manifest.json`` 才拿得到，而那份文件名是 loader 的常量，为日志可辨识度
        复制一份真相源不值；发现流程里目录与插件一一对应，配合 ``path`` 足以定位。
    :param model: 该插件声明的配置模型。
    :return: 已校验的配置实例。读取失败、写入失败都只记 warning，本次按内存中
        的对账结果运行，不抛异常——一个插件的配置写错不该让宿主的发现流程中断。
    副作用：文件不存在时写入一份新的 ``config.toml``；已存在且模型有新增字段时
        向 ``[plugin]`` 段末尾追加这些字段。
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

    fields = model.model_fields
    retired = [key for key in section if key not in fields]
    if retired:
        # 退休键只报告不删除：删除不可逆，文件里原样留着，由人决定去留。
        logger.warning(
            '插件配置里这些键已没有代码读取，原样保留在文件中',
            plugin=directory.name,
            keys=retired,
            path=str(path),
        )

    # 必填字段（无默认值）不补：补一个假默认值会掩盖「该由用户做决定」这件事。
    missing = [name for name, field in fields.items()
               if name not in section and not field.is_required()]
    if missing:
        try:
            appended = _append_missing_fields(path, model, missing)
        except OSError as exc:
            logger.warning(
                '插件配置补齐写入失败，本次按内存中的对账结果运行',
                plugin=directory.name,
                keys=missing,
                path=str(path),
                error=str(exc),
            )
        else:
            if appended:
                logger.info(
                    '已把插件的新增配置项补进文件',
                    plugin=directory.name,
                    keys=missing,
                    path=str(path),
                )

    known = {key: value for key, value in section.items() if key in fields}
    return _validate_section(model, known, directory.name)


def _validate_section(
    model: type[PluginConfig],
    values: Dict[str, Any],
    plugin_id: str,
) -> PluginConfig:
    """逐字段校验用户写的值，非法字段单独退回默认值并报告，不牵连其余字段。

    :param model: 配置模型。
    :param values: ``[plugin]`` 段里模型认识的键值。
    :param plugin_id: 插件 id，用于日志定位。
    :return: 校验通过的配置实例；校验错误无法定位到具体字段（模型级校验器、
        必填字段缺失）时退回全默认值并记 warning。
    副作用：记日志。
    """
    candidate = dict(values)
    while True:
        try:
            return model.model_validate(candidate)
        except ValidationError as exc:
            bad: Dict[str, str] = {}
            for error in exc.errors():
                location = error['loc']
                if location and isinstance(location[0], str) and location[0] in candidate:
                    bad.setdefault(location[0], error['msg'])
            if not bad:
                logger.warning(
                    '插件配置校验失败，按默认值运行',
                    plugin=plugin_id,
                    error=str(exc),
                )
                return model()
            for name, message in bad.items():
                value = candidate.pop(name)
                logger.warning(
                    '插件配置字段的值非法，该字段按默认值运行',
                    plugin=plugin_id,
                    field=name,
                    expected=_expected_type(model, name),
                    actual=repr(value),
                    detail=message,
                )


def _expected_type(model: type[PluginConfig], name: str) -> str:
    """给出字段注解的可读形式，用于「期望什么」的日志。

    :param model: 配置模型。
    :param name: 字段名。
    :return: 注解的类型名；组合注解没有 ``__name__`` 时退回 ``str()``。
    副作用：无。
    """
    annotation = model.model_fields[name].annotation
    return getattr(annotation, '__name__', None) or str(annotation)


# ``[plugin]`` 表头允许空白与引号（TOML 里均合法），行尾可带注释。
_PLUGIN_HEADER = re.compile(r'^\s*\[\s*["\']?plugin["\']?\s*\]\s*(?:#.*)?$')


def _append_missing_fields(
    path: Path,
    model: type[PluginConfig],
    missing: List[str],
) -> bool:
    """把模型新增、文件里还没有的字段追加进 ``[plugin]`` 段末尾。

    刻意用行扫描而不是「解析后整份重写」：后者会丢掉用户写的注释与排版，而这份
    文件是人要读的（与主配置 ``apply_added_fields`` 同一纪律）。字段的
    ``description`` 注释与默认值渲染复用 :func:`render_plugin_config` 的那套写法，
    不另造第二份。

    :param path: 目标 ``config.toml``。
    :param model: 配置模型。
    :param missing: 待补齐的字段名。
    :return: 是否真的写了文件。``[plugin]`` 段由点分键拼出、找不到表头行时返回
        ``False``——末尾追加第二张同名表会把文件改到无法解析，不如不补。
    :raises OSError: 读写文件失败。
    副作用：向文件追加行；已有的行、注释、顺序一律不动，行尾序列保持原样。
    """
    text = path.read_text(encoding='utf-8')
    newline = '\r\n' if '\r\n' in text else '\n'
    lines = text.splitlines()
    header_at = next(
        (index for index, line in enumerate(lines) if _PLUGIN_HEADER.match(line)),
        None,
    )
    if header_at is None:
        return False
    insert_at = header_at + 1
    while insert_at < len(lines) and not lines[insert_at].lstrip().startswith('['):
        insert_at += 1
    # 段尾的空行留在追加块之后，追加块紧跟段内最后一行有效内容。
    while insert_at > header_at + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    block: List[str] = [''] if insert_at > header_at + 1 else []
    for name in missing:
        field = model.model_fields[name]
        description = (field.description or '').strip()
        if description:
            block.append(f'# {description}')
        block.append(f'{name} = {_toml_value(field.get_default(call_default_factory=True))}')
    if insert_at < len(lines):
        block.append('')
    lines[insert_at:insert_at] = block
    new_text = newline.join(lines)
    if text.endswith(('\n', '\r')):
        new_text += newline
    path.write_text(new_text, encoding='utf-8')
    return True


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
    if isinstance(value, BaseModel):
        raise TypeError(
            f'插件配置暂不支持嵌套模型（{type(value).__name__}）：嵌套模型的字段说明'
            '与默认值无法平铺进 [plugin] 段，需要嵌套结构时请改用 dict 字段或平铺声明'
        )
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
