"""对账配置文件与 schema，补齐新增字段并把差异展示在控制台。

解决的是版本升级时的两个沉默问题：

1. **新增的配置项用户看不见**。代码里加了字段、给了默认值，用户的 TOML 里没有那一行，
   于是「这个功能能不能配、默认是什么」只能去翻源码。
2. **废弃的配置项留在文件里**。改名或删掉的字段还躺在用户文件里，看着像生效，实际
   已经没有任何代码读它——比没写更容易误导。

处置口径：

- **新增字段补进文件并写默认值**，只追加不改写——已有的行、注释、顺序一律不动。
- **废弃字段只报告不删除**。删掉是不可逆的，而用户可能正打算改回去；报出来由人决定。
- **写任何一个字节之前先整目录备份**到 ``data/backups/config/<时间戳>/``。``config/``
  含明文密钥且没有版本控制，改坏一次没有第二份。

对外暴露 :func:`upgrade_config_directory`，由 ``loader.load_config`` 在解析之前调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple, Type, get_args, get_origin

import shutil
import tomllib
import types
import typing

from pydantic import BaseModel

from src.core.common.console_layout import print_box
from src.core.common.logger import get_logger

logger = get_logger(__name__)

# 版本段由 read_versioned_toml 单独校验，不参与字段对账。
_SKIP_SECTIONS = frozenset({'inner'})


@dataclass
class AddedField:
    """表示一个 schema 里有、用户文件里没有的配置项。

    :ivar section: 所属 TOML 表名；顶层字段为空字符串。
    :ivar key: 字段名。
    :ivar value: 已编码为 TOML 字面量的默认值。
    """

    section: str
    key: str
    value: str

    @property
    def path(self) -> str:
        """返回 ``段.字段`` 形式的完整路径，顶层字段只返回字段名。"""

        return f'{self.section}.{self.key}' if self.section else self.key


@dataclass
class FileDiff:
    """单个配置文件与 schema 的差异。

    :ivar name: 文件名，用于展示。
    :ivar added: 需要补进文件的新增字段。
    :ivar removed: 文件里有但 schema 已不认识的字段路径，只报告不删除。
    """

    name: str
    added: List[AddedField] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """判断该文件是否既无新增也无废弃字段。"""

        return not self.added and not self.removed


def _encode(value: Any) -> str:
    """把标量或简单容器编码为 TOML 字面量。

    :param value: 待编码的默认值。
    :return: 可直接写进 TOML 的文本。
    :raises TypeError: 遇到无法编码的类型——那说明该字段不该走自动补齐。
    副作用：无。
    """

    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_encode(item) for item in value) + ']'
    raise TypeError(f'无法编码为 TOML 字面量：{value!r}')


def _model_of(annotation: Any) -> Type[BaseModel] | None:
    """从字段注解里取出 BaseModel 子类，包含 ``Optional[Model]`` 的情形。

    :param annotation: pydantic 字段的类型注解。
    :return: 对应的模型类；注解不是模型（或是模型的容器）时返回 ``None``。
    副作用：无。
    """

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        models = [_model_of(arg) for arg in get_args(annotation)]
        found = [m for m in models if m is not None]
        return found[0] if len(found) == 1 else None
    return None


def _default_of(info: Any) -> Any:
    """取字段默认值；工厂默认值会被调用一次。

    :param info: pydantic ``FieldInfo``。
    :return: 默认值；字段必填时返回 :data:`Ellipsis` 表示「没有默认值」。
    副作用：调用 ``default_factory``，因此该工厂必须无副作用（pydantic 本身也这么假设）。
    """

    if info.default_factory is not None:
        return info.default_factory()
    return info.default


def _diff_section(
    raw: Dict[str, Any],
    model: Type[BaseModel],
    section: str,
) -> Tuple[List[AddedField], List[str]]:
    """比较一层 TOML 表与对应模型的字段集合。

    嵌套模型递归下去；**列表字段一律当叶子跳过**——`api_providers` / `models` 那两个
    是用户数据不是设置项，往里补默认值只会凭空造出一个不存在的厂商。

    :param raw: 该层的原始 TOML 字典。
    :param model: 对应的 pydantic 模型类。
    :param section: 当前表名，用于拼接展示路径。
    :return: ``(新增字段, 废弃字段路径)`` 二元组。
    副作用：无。
    """

    added: List[AddedField] = []
    removed: List[str] = []
    fields = model.model_fields
    for name, info in fields.items():
        nested = _model_of(info.annotation)
        if nested is not None:
            child_raw = raw.get(name)
            if isinstance(child_raw, dict):
                child_section = f'{section}.{name}' if section else name
                child_added, child_removed = _diff_section(child_raw, nested, child_section)
                added.extend(child_added)
                removed.extend(child_removed)
            # 整段缺失时不逐字段补：那是新增一整块功能配置，交给人按文档写，
            # 自动铺一段默认值反而让用户以为自己配过。
            continue
        if name in raw:
            continue
        default = _default_of(info)
        if default is Ellipsis:
            # 必填字段缺失会在 model_validate 时报错，不该被静默补一个假默认值。
            continue
        try:
            encoded = _encode(default)
        except TypeError:
            continue
        added.append(AddedField(section=section, key=name, value=encoded))
    for key in raw:
        if key in fields or key in _SKIP_SECTIONS:
            continue
        if isinstance(raw[key], dict) and not any(
            _model_of(info.annotation) is not None and field_name == key
            for field_name, info in fields.items()
        ):
            removed.append(f'{section}.{key}' if section else key)
            continue
        if key not in fields:
            removed.append(f'{section}.{key}' if section else key)
    return added, removed


def diff_document(raw: Dict[str, Any], model: Type[BaseModel], name: str) -> FileDiff:
    """对账一份配置文件与它的文档模型。

    :param raw: ``tomllib`` 解析出的原始字典。
    :param model: 该文件对应的文档模型类。
    :param name: 文件名，用于展示。
    :return: 该文件的差异。
    副作用：无。
    """

    added, removed = _diff_section(raw, model, '')
    return FileDiff(name=name, added=added, removed=removed)


def backup_config_directory(directory: Path, data_dir: Path) -> Path:
    """在改写任何配置文件之前，整目录快照一份。

    ``config/`` 含明文密钥且不在版本控制里，改坏没有第二份，因此备份是写入的前置条件
    而不是可选项——与数据库迁移前 ``backup()`` 同一条纪律。

    :param directory: 配置目录。
    :param data_dir: 运行时数据目录，备份落在它的 ``backups/config/`` 下。
    :return: 本次备份目录的路径。
    :raises OSError: 创建目录或复制文件失败——此时**不允许**继续写配置。
    副作用：在 ``data/backups/config/<时间戳>/`` 下复制整个配置目录。
    """

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = data_dir / 'backups' / 'config' / stamp
    shutil.copytree(directory, dest)
    logger.info('config_backup_created', dest=str(dest))
    return dest


def apply_added_fields(path: Path, added: List[AddedField]) -> None:
    """把新增字段追加进对应的 TOML 表，不触碰任何已有行。

    实现刻意用行扫描而不是「解析后整份重写」：后者会丢掉用户写的注释与排版，而这份
    文件是人要读的。追加位置取该表的最后一个非空行之后，段不存在时在文件末尾新建。

    :param path: 目标 TOML 文件。
    :param added: 待追加的字段。
    :return: 无返回值。
    :raises OSError: 读写文件失败。
    副作用：改写目标文件；调用前必须已经完成 :func:`backup_config_directory`。
    """

    if not added:
        return
    lines = path.read_text(encoding='utf-8').splitlines()
    by_section: Dict[str, List[AddedField]] = {}
    for item in added:
        by_section.setdefault(item.section, []).append(item)

    for section, items in by_section.items():
        header = f'[{section}]'
        insert_at = -1
        if section:
            for index, line in enumerate(lines):
                if line.strip() == header:
                    insert_at = index + 1
                    while insert_at < len(lines) and not lines[insert_at].lstrip().startswith('['):
                        insert_at += 1
                    break
        else:
            # 顶层字段必须排在第一个表头之前，否则会被归进那个表。
            insert_at = next(
                (i for i, line in enumerate(lines) if line.lstrip().startswith('[')),
                len(lines),
            )
        block = ['# 本项由版本升级自动补齐，值为默认值', *[f'{i.key} = {i.value}' for i in items]]
        if insert_at < 0:
            lines.extend(['', header, *block])
            continue
        # 先吃掉原有的尾部空行再插入，避免与下一个表头之间出现两种间距。
        while insert_at > 0 and not lines[insert_at - 1].strip():
            insert_at -= 1
        tail = [''] if insert_at < len(lines) else []
        lines[insert_at:insert_at] = ['', *block, *tail]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def report_config_changes(diffs: List[FileDiff]) -> None:
    """把配置差异逐条打印成控制台信息框。

    没有任何差异时不打印——每次启动刷一个「无变化」的框，真正有变化的那次就会被淹没。

    :param diffs: 各文件的差异。
    :return: 无返回值。
    副作用：向 stdout 打印信息框并记日志。
    """

    interesting = [d for d in diffs if not d.is_empty()]
    if not interesting:
        return
    rows: List[str] = []
    for diff in interesting:
        for item in diff.added:
            rows.append(f'{diff.name} 新增 {item.path} = {item.value}（默认值，已写入文件）')
        for path in diff.removed:
            rows.append(f'{diff.name} 已废弃 {path}（代码不再读取，文件里保留，需要你确认后手删）')
    print_box('配置字段变更', rows, width=104)
    logger.info(
        'config_fields_changed',
        added=[f'{d.name}:{i.path}' for d in interesting for i in d.added],
        removed=[f'{d.name}:{p}' for d in interesting for p in d.removed],
    )


def upgrade_config_directory(
    directory: Path,
    documents: Dict[str, Type[BaseModel]],
    data_dir: Path,
) -> List[FileDiff]:
    """对账整个配置目录，补齐新增字段并展示差异。

    :param directory: 配置目录。
    :param documents: 文件名到文档模型类的映射。
    :param data_dir: 运行时数据目录，用于放备份。
    :return: 各文件的差异列表，供调用方按需再加工。
    :raises OSError: 备份或写入失败——不吞异常，配置写坏比启动失败严重得多。
    副作用：可能备份配置目录并向文件追加字段，并向控制台打印差异。
    """

    diffs: List[FileDiff] = []
    for name, model in documents.items():
        path = directory / name
        if not path.is_file():
            continue
        raw = tomllib.loads(path.read_text(encoding='utf-8'))
        diffs.append(diff_document(raw, model, name))

    if any(diff.added for diff in diffs):
        backup_config_directory(directory, data_dir)
        for diff in diffs:
            apply_added_fields(directory / diff.name, diff.added)
    report_config_changes(diffs)
    return diffs
