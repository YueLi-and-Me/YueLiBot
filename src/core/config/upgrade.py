"""对账配置文件与 schema，补齐新增字段并把差异展示在控制台。

解决的是版本升级时的两个沉默问题：

1. 新增的配置项用户不可见：代码里加了字段、给了默认值，用户的 TOML 里没有那一行，
   能否配置、默认是什么无从得知。
2. 废弃的配置项留在文件里：改名或删掉的字段仍在用户文件中，看似生效，
   实际已经没有任何代码读取，比缺失更易误导。

处置口径：

- 新增字段补进文件并写默认值，只追加不改写：已有的行、注释、顺序一律不动。
- 废弃字段就地删除并展示实际删除的路径：留在文件里的字段看似生效，实际没有代码读取。
- 字段对账成功后再把 ``[inner].version`` 改写为当前配置版本；版本号最后写，
  避免中途失败留下「版本已新、字段还旧」的配置。
- 写入任何字节之前先整目录备份到 ``data/backups/config/<时间戳>/``。``config/``
  含明文密钥且没有版本控制。

对外暴露 :func:`upgrade_config_directory`，由 ``src.main`` 在加载配置之前调用；
不是 ``loader.load_config`` 内部调用的——加载器只管解析与交叉校验，补字段发生在它之前。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple, Type, get_args, get_origin

import re
import shutil
import tomllib
import types
import typing

from pydantic import BaseModel

from src.core.config.schema import CONFIG_VERSION
from src.core.logging.console_layout import print_box
from src.core.logging.logger import get_logger

logger = get_logger(__name__)

# 版本段不参与字段增删：它的值由 read_versioned_toml 单独校验，升级器在字段对账
# 全部写成功之后才通过 _rewrite_version 改写，避免出现「版本已新、字段还旧」。
_SKIP_SECTIONS = frozenset({'inner'})


def _comment_start(line: str) -> int:
    """返回一行 TOML 中行内注释的起始下标，找不到时返回行长度。

    不能用 ``str.split('#', 1)``：TOML 的引号字符串里 ``#`` 是正文而不是注释。
    例如 ``fallback_theme = "按自己的节奏 # 度过今天"`` 的前一个 ``#`` 不能被当成
    注释起点，否则升级回写时会把用户配置值截断。本函数逐字符扫描，只在单行
    基本字符串（双引号，支持反斜杠转义）或字面量字符串（单引号，无转义）之外
    才把 ``#`` 判为注释起点。

    :param line: 一行 TOML 原文（可包含行尾换行符，但不要求）。
    :return: ``#`` 在行内的下标；该行没有行内注释时返回 ``len(line)``。
    副作用：无。
    """

    quote = ''
    escaped = False
    for index, char in enumerate(line):
        if quote:
            if quote == '"' and char == '\\' and not escaped:
                escaped = True
                continue
            if char == quote and not escaped:
                quote = ''
            escaped = False
            continue
        if char in ('"', "'"):
            quote = char
        elif char == '#':
            return index
    return len(line)


def _normalize_section_name(raw: str) -> str:
    """把方括号内的 TOML 表名规范化为点分路径。

    表名允许写成 ``[ typing . follow_up ]`` 或 ``["a.b"]`` 这类形态；字段对账生成
    的路径永远是 ``typing.follow_up`` 这种紧凑点分形式，因此这里去掉各段两端的
    空白和引号后再拼回点分路径，保证两条路径能对上同一张表。

    :param raw: 已去掉首尾方括号的原始表名文本。
    :return: 规范化后的点分表名。
    副作用：无。
    """

    parts: list[str] = []
    current: list[str] = []
    quote = ''
    escaped = False
    for char in raw:
        if quote:
            current.append(char)
            if quote == '"' and char == '\\' and not escaped:
                escaped = True
                continue
            if char == quote and not escaped:
                quote = ''
            escaped = False
            continue
        if char in ('"', "'"):
            quote = char
            current.append(char)
        elif char == '.':
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append(''.join(current).strip())
    return '.'.join(
        part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in ('"', "'") else part
        for part in parts
    )


def _section_name(line: str) -> str | None:
    """识别一行是否是 TOML 表标题，并返回规范化后的表名。

    本项目所有表标题都带行内注释（例如 ``[schedule] # 每日方向与活动：...``）。
    旧实现按「整行以 ``]`` 结尾」判断，带注释时永远匹配不到目标表，增量路径会
    在文件末尾新建一个同名表、把配置写成非法 TOML；删除路径则把段名读成空串，
    废弃字段永远删不掉。这里先剥离行内注释再判断，两条路径共用同一实现。

    :param line: 一行 TOML 原文。
    :return: 规范化的表名；该行不是表标题时返回 ``None``。
    副作用：无。
    """

    content = line[: _comment_start(line)].strip()
    if not content.startswith('['):
        return None
    # 只接受「单个闭合方括号后没有非注释正文」的行，防止把数组值行误判成表标题。
    if not content.endswith(']'):
        return None
    inner = content[1:-1].strip()
    return _normalize_section_name(inner) if inner else None


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
    :ivar removed: 文件里有但 schema 已不认识的字段路径；升级时就地删除，
        这里保留的是实际删除的路径，用于展示。
    :ivar version_from: 升级前 ``[inner].version``；文件缺少版本段时为 ``None``。
    :ivar version_to: 实际写入的当前配置版本；本次没有改写版本号时为 ``None``。
    :ivar failure: 本次升级未完成时的中文原因；成功或无需升级时为 ``None``。
    """

    name: str
    added: List[AddedField] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    version_from: str | None = None
    version_to: str | None = None
    failure: str | None = None

    def version_upgraded(self) -> bool:
        """判断本次升级是否真的改写了该文件的配置版本号。"""

        return self.version_to is not None and self.version_to != self.version_from

    def is_empty(self) -> bool:
        """判断该文件是否既无字段差异、也没有本次完成的版本号升级或失败。"""

        return (
            not self.added
            and not self.removed
            and not self.version_upgraded()
            and self.failure is None
        )


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

    嵌套模型递归下去；列表字段一律当叶子跳过：`api_providers` / `models` 是
    用户数据不是设置项，补默认值会产生不存在的厂商条目。

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
    :raises OSError: 创建目录或复制文件失败——此时不允许继续写配置。
    副作用：在 ``data/backups/config/<时间戳>/`` 下复制整个配置目录。
    """

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = data_dir / 'backups' / 'config' / stamp
    shutil.copytree(directory, dest)
    logger.info('config_backup_created', dest=str(dest))
    return dest


def apply_added_fields(path: Path, added: List[AddedField]) -> bool:
    """把新增字段追加进对应的 TOML 表，不触碰任何已有行。

    实现刻意用行扫描而不是「解析后整份重写」：后者会丢掉用户写的注释与排版，而这份
    文件是人要读的。追加位置取该表的最后一个非空行之后，段不存在时在文件末尾新建。

    段标题统一由 :func:`_section_name` 解析：本项目所有表标题都带行内注释，旧实现
    用「整行等于 ``[段名]``」判等，在带注释的段里补字段时找不到目标段，就会在文件
    末尾新建同名表。实测后果是用户配置被写成非法 TOML，下次启动直接
    ``TOMLDecodeError``。这是既有缺陷，与本次新增的 ``energy_enabled`` 无关：任何
    一次在带注释段里新增字段都会踩，之前没暴露只是因为没有无头升级路径走到这里。

    写盘之前会先回读校验；与 :func:`apply_removed_fields` 同一纪律，解析不过就整份
    保留原样，宁可少补一个字段也不把配置写成起不来的样子。

    :param path: 目标 TOML 文件。
    :param added: 待追加的字段。
    :return: 确实写入且回读解析通过返回 ``True``；没有待补字段或回读失败返回 ``False``。
    :raises OSError: 读写文件失败。
    副作用：可能改写目标文件；调用前必须已经完成 :func:`backup_config_directory`。
    """

    if not added:
        return False
    original = path.read_text(encoding='utf-8')
    lines = original.splitlines()
    by_section: Dict[str, List[AddedField]] = {}
    for item in added:
        by_section.setdefault(item.section, []).append(item)

    for section, items in by_section.items():
        header = f'[{section}]'
        insert_at = -1
        if section:
            for index, line in enumerate(lines):
                if _section_name(line) == section:
                    insert_at = index + 1
                    while insert_at < len(lines) and _section_name(lines[insert_at]) is None:
                        insert_at += 1
                    break
        else:
            # 顶层字段必须排在第一个表头之前，否则会被归进那个表。
            insert_at = next(
                (i for i, line in enumerate(lines) if _section_name(line) is not None),
                len(lines),
            )
        block = ['# 本项由版本升级自动补齐，值为默认值', *[f'{i.key} = {i.value}' for i in items]]
        if insert_at < 0:
            lines.extend(['', header, *block])
            continue
        # 先移除原有的尾部空行再插入，避免与下一个表头之间出现两种间距。
        while insert_at > 0 and not lines[insert_at - 1].strip():
            insert_at -= 1
        tail = [''] if insert_at < len(lines) else []
        lines[insert_at:insert_at] = ['', *block, *tail]
    text = '\n'.join(lines) + '\n'
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        logger.warning(
            'config_add_reverted',
            file=path.name,
            fields=[item.path for item in added],
        )
        return False
    path.write_text(text, encoding='utf-8')
    return True


def apply_removed_fields(path: Path, removed: List[str]) -> List[str]:
    """把代码已经不再读取的字段从 TOML 里删掉。

    改动前这些字段只被报告、从不删除，导致每次启动都重报同一份清单，
    而该信息框的用途是展示本次启动的变更。

    做法：按行删（与 :func:`apply_added_fields` 同一纪律，重写整份会丢掉用户的
    注释与排版），删完重新解析一遍；解析不过就整份还原，当作未删。有这道
    回读校验兜底，删除逻辑本身不必处理跨行数组、引号内的方括号等边角情况：
    真遇到即还原，不会把配置改到无法启动。

    段标题与增量路径共用 :func:`_section_name`。旧实现要求标题行以 ``]`` 结尾，
    带行内注释的标题会被读成空表名，于是 ``段.字段`` 永远对不上，废弃字段删不掉；
    这是同一个既有缺陷的另一半，与本次业务字段无关。

    注释一律不动：孤立的注释无害，误删用户自己写的说明无法挽回。

    :param path: 目标 TOML 文件。
    :param removed: 废弃字段的点分路径，例如 ``schedule.min_slots``；整段废弃时传表名。
    :return: 实际删掉的路径；文件里已经没有、或删后解析失败时返回空列表。
    :raises OSError: 读写文件失败。
    副作用：改写目标文件；调用前必须已经完成 :func:`backup_config_directory`。
    """

    if not removed:
        return []
    original = path.read_text(encoding='utf-8')
    lines = original.splitlines()
    wanted = set(removed)

    kept: List[str] = []
    dropped: List[str] = []
    section = ''
    dropping_section = False
    for line in lines:
        name = _section_name(line)
        if name is not None:
            section = name
            dropping_section = section in wanted
            if dropping_section:
                dropped.append(section)
                continue
        elif dropping_section:
            continue
        else:
            match = re.match(r'^\s*(?:"([^"]+)"|\'([^\']+)\'|([A-Za-z0-9_\-]+))\s*=', line)
            key = (match.group(1) or match.group(2) or match.group(3)) if match else None
            full = f'{section}.{key}' if section and key else key
            if full in wanted:
                dropped.append(full)
                continue
        kept.append(line)

    if not dropped:
        return []
    text = '\n'.join(kept).rstrip('\n') + '\n'
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # 删出了语法错误（多半是跨行值只删掉了首行）：原样留着，交回给报告去提示。
        logger.warning('config_prune_reverted', file=path.name, fields=dropped)
        return []
    path.write_text(text, encoding='utf-8')
    return dropped


def _rewrite_version(path: Path, version: str) -> bool:
    """把 ``[inner].version`` 就地改写为当前配置版本，保留其余字节。

    只在字段增删全部成功之后调用，理由见 :func:`upgrade_config_directory`：若先改
    版本号再写字段，中途失败会得到「版本已新、字段还旧」的配置，比旧版本更难恢复。
    版本号是字符串字面量，此处仍按行扫描而不是整份重写，避免丢掉用户注释与排版；
    改写后先回读解析，失败则不落盘并返回 ``False``。

    跨版本说明：本项目的字段对账是「文件现有字段与当前模型求差」，不按来源版本
    逐级迁移。因此 1.4.0 直接改写成 1.6.0 与经过中间版本是同一结果——差集会把
    中间版本新增字段补齐、废弃字段删掉。这里不存在「1.4 到 1.5 的迁移路径」这种
    概念，后来者不要按逐级迁移链去设计或寻找它。

    :param path: 目标 TOML 文件。
    :param version: 要写入的配置版本号，例如 ``CONFIG_VERSION``。
    :return: 确实改写了版本号并回读通过返回 ``True``；没有找到版本字段、版本已是
        目标值或回读失败返回 ``False``。
    :raises OSError: 读写文件失败。
    副作用：可能改写目标文件；调用前必须已经完成 :func:`backup_config_directory`。
    """

    original = path.read_text(encoding='utf-8')
    lines = original.splitlines()
    section = ''
    changed = False
    for index, line in enumerate(lines):
        name = _section_name(line)
        if name is not None:
            section = name
            continue
        if section != 'inner':
            continue
        cut = _comment_start(line)
        content = line[:cut]
        match = re.match(r'^(\s*version\s*=\s*)(.*?)(\s*)$', content)
        if match is None:
            continue
        raw_value = match.group(2).strip()
        quote = raw_value[0] if raw_value[:1] in ('"', "'") and raw_value.endswith(raw_value[:1]) else ''
        literal = f'{quote}{version}{quote}' if quote else f'"{version}"'
        if raw_value == literal:
            return False
        lines[index] = f'{match.group(1)}{literal}{match.group(3)}{line[cut:]}'
        changed = True
        break
    if not changed:
        return False
    text = '\n'.join(lines) + '\n'
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        logger.warning('config_version_rewrite_reverted', file=path.name, version=version)
        return False
    path.write_text(text, encoding='utf-8')
    return True


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
        if diff.failure is not None:
            rows.append(f'{diff.name} 升级未完成（{diff.failure}），文件保持原样，详见上方警告')
            continue
        if diff.version_upgraded():
            rows.append(
                f'{diff.name} 配置版本 {diff.version_from} -> {diff.version_to}（已写入文件）'
            )
        for item in diff.added:
            rows.append(f'{diff.name} 新增 {item.path} = {item.value}（默认值，已写入文件）')
        for path in diff.removed:
            rows.append(f'{diff.name} 删除 {path}（代码已不再读取，旧值见本次配置备份）')
    print_box('配置变更', rows, width=104, source=__name__)
    logger.info(
        'config_fields_changed',
        added=[f'{d.name}:{i.path}' for d in interesting for i in d.added],
        removed=[f'{d.name}:{p}' for d in interesting for p in d.removed],
        versions=[
            f'{d.name}:{d.version_from}->{d.version_to}'
            for d in interesting if d.version_upgraded()
        ],
        failures=[f'{d.name}:{d.failure}' for d in interesting if d.failure is not None],
    )


def _upgrade_document(path: Path, diff: FileDiff, target_version: str) -> bool:
    """对一份 TOML 完成字段增删与版本号改写，任何一步失败都整份还原。

    顺序固定为「补新增字段 -> 删废弃字段 -> 整体回读 -> 改写版本号 -> 再回读」。
    版本号必须最后写：如果先写成新版本再写字段，中途失败会留下「版本已新、字段
    还旧」的配置，加载器会按新结构解释旧字段，比停在旧版本更难恢复。

    :param path: 目标 TOML 文件。
    :param diff: 该文件与当前模型的差异；成功时会被补充实际删除字段与版本号。
    :param target_version: 要写入的配置版本，通常是当前 ``CONFIG_VERSION``。
    :return: 全部步骤成功返回 ``True``；某一步回读失败、已整份还原时返回 ``False``。
    :raises OSError: 读写文件失败；调用方已完成整目录备份，异常必须向上暴露。
    副作用：可能改写目标文件；失败时把文件恢复到进入本函数时的原文。
    """

    original = path.read_text(encoding='utf-8')
    try:
        if diff.added and not apply_added_fields(path, diff.added):
            path.write_text(original, encoding='utf-8')
            diff.failure = '新增字段未通过回读校验'
            return False
        if diff.removed:
            dropped = apply_removed_fields(path, diff.removed)
            if set(dropped) != set(diff.removed):
                path.write_text(original, encoding='utf-8')
                diff.failure = '废弃字段未全部删除'
                return False
            diff.removed = dropped
        # 增删叠加后的第一次整体回读；通过才允许动版本号。
        tomllib.loads(path.read_text(encoding='utf-8'))
        if diff.version_from != target_version:
            if not _rewrite_version(path, target_version):
                path.write_text(original, encoding='utf-8')
                diff.failure = f'配置版本未从 {diff.version_from!r} 改写为 {target_version}'
                return False
            diff.version_to = target_version
        # 第二次回读是 report_config_changes 宣称「已写入文件」的依据。
        tomllib.loads(path.read_text(encoding='utf-8'))
        return True
    except tomllib.TOMLDecodeError as exc:
        path.write_text(original, encoding='utf-8')
        diff.failure = f'升级结果回读解析失败：{exc}'
        logger.warning(
            'config_upgrade_reverted',
            file=path.name,
            reason=str(exc),
        )
        return False
    except Exception:
        # 非语法类异常已经无法安全继续；先恢复原文，再把异常交给调用方。
        path.write_text(original, encoding='utf-8')
        raise


def upgrade_config_directory(
    directory: Path,
    documents: Dict[str, Type[BaseModel]],
    data_dir: Path,
) -> List[FileDiff]:
    """对账整个配置目录，补齐新增字段、删除废弃字段并把版本号升到当前值。

    本次升级与来源版本无关：字段差异是「文件现有字段与当前模型求差」，所以 1.4.0
    可以直接升到 1.6.0，不存在逐级迁移链；版本号只是在字段对账成功之后统一改写，
    不需要知道文件原来属于哪个中间版本。这个前提必须保持：一旦改为逐级迁移，
    这里就需要来源版本分派，不能只改 ``version`` 字段。

    只有确实需要写入（有字段差异，或 ``[inner].version`` 不是当前值）时才做整目录
    备份；备份是任何字节写入的前置条件，与数据库迁移同一条纪律。单个文件升级失败
    时保留原文并继续处理其余文件，失败原因写进返回的 :class:`FileDiff` 并由
    :func:`report_config_changes` 打到控制台。

    :param directory: 配置目录。
    :param documents: 文件名到文档模型类的映射。
    :param data_dir: 运行时数据目录，用于放备份。
    :return: 各文件的差异列表，供调用方按需再加工。
    :raises OSError: 备份、读取或写入失败——不吞异常，配置写坏比启动失败严重得多。
    副作用：可能备份配置目录、改写文件并向控制台打印差异。
    """

    diffs: List[FileDiff] = []
    targets: List[FileDiff] = []
    for name, model in documents.items():
        path = directory / name
        if not path.is_file():
            continue
        raw = tomllib.loads(path.read_text(encoding='utf-8'))
        diff = diff_document(raw, model, name)
        inner = raw.get('inner')
        version = inner.get('version') if isinstance(inner, dict) else None
        diff.version_from = version if isinstance(version, str) else None
        diffs.append(diff)
        if diff.added or diff.removed or diff.version_from != CONFIG_VERSION:
            targets.append(diff)

    if targets:
        backup_config_directory(directory, data_dir)
        for diff in targets:
            _upgrade_document(directory / diff.name, diff, CONFIG_VERSION)
    report_config_changes(diffs)
    return diffs
