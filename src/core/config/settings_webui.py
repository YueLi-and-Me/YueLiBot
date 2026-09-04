"""按声明式 schema 读取并保存运行时 TOML 配置文件的 WebUI 工作台。

模块加载 ``settings_schema.json`` 作为字段、类型、中文说明与文件布局的唯一
映射来源；读取时用 Pydantic 模型补齐默认值并隐藏密钥，保存时先写临时目录、
通过完整启动校验后再原子替换原文件。

配置分两类：主体的四个文件都在配置目录内；QQ 适配器的连接配置属于适配器自身，
位于 ``adapters/<当前适配器>/config.toml``，由 ``adapter_selection`` 定位、按该
适配器清单声明的连接段名读写。schema 里它以稳定的逻辑标识 ``adapter.toml``
出现，与磁盘文件名无关。
"""

from __future__ import annotations

from pathlib import Path
from pydantic import BaseModel
from typing import Any, Dict, List

import copy
import json
import os
import tempfile

from .adapter_selection import (
    adapter_config_path,
    adapter_config_section,
    read_active_adapter,
)
from .loader import CONFIG_VERSION, _load_split_config
from .schema import GenerationConfig, ModelTaskConfig
from .model_webui import (
    _normalize_generation,
    _normalize_models,
    _normalize_providers,
    _normalize_tasks,
    snapshot as model_snapshot,
)
from .schema import BotDocument, FeatureDocument, ModelCatalog, ProviderCatalog
from .toml_io import read_versioned_toml
from src.core.common.logger import get_logger
from src.platforms.onebot11.config import (
    PROTOCOL_SECTION_FIELD,
    AdapterDocument,
    NAPCAT_CONFIG_VERSION,
    read_section_config,
)

logger = get_logger(__name__)

_SCHEMA_PATH = Path(__file__).with_name('settings_schema.json')
_VERSION_HINT = '正常情况下 Electron 启动时会自动升级，手工改过的话请对照模板补齐'
# 配置目录内的主体文件，保存时在同一目录里原子替换。
_CONFIG_FILES = (
    'bot.toml', 'features.toml', 'providers.toml', 'models.toml',
)
# 适配器连接配置在 schema 与前端载荷里的稳定标识。磁盘文件名随当前适配器变化，
# 不能拿它当标识：前端按标识索引字段值，标识一变原有表单状态全部对不上。
_ADAPTER_FILE = 'adapter.toml'
_schema: Dict[str, Any] | None = None

def load_schema() -> Dict[str, Any]:
    """读取并缓存 WebUI 设置字段映射文件。

    :return: 包含 files 列表的 schema 字典。
    :raises OSError: 映射文件不存在或无法读取。
    :raises json.JSONDecodeError: 映射文件不是合法 JSON。
    副作用：首次调用后缓存解析结果。
    """
    global _schema
    if _schema is None:
        _schema = json.loads(_SCHEMA_PATH.read_text(encoding='utf-8'))
        _require_task_entries_match_config(_schema)
        _require_sections_match_models(_schema)
    return _schema


def _section_model(document: type[BaseModel], key: str) -> type[BaseModel] | None:
    """按 schema 的段名解析出它对应的配置模型。

    段名允许带点（``typing.nudge``），对应模型上的逐层字段。

    :param document: 该配置文件的顶层文档模型。
    :param key: schema 里的段名。
    :return: 对应的模型类；路径走不通或终点不是模型时返回 ``None``。
    """
    current: Any = document
    for part in key.split('.'):
        fields = getattr(current, 'model_fields', None)
        if not fields or part not in fields:
            return None
        current = fields[part].annotation
    return current if isinstance(current, type) and issubclass(current, BaseModel) else None


def _require_sections_match_models(schema: Dict[str, Any]) -> None:
    """校验每个配置段声明的字段与其配置模型完全一致。

    与 :func:`_require_task_entries_match_config` 是同一个故障的两个面：写盘只
    认 schema 里列出的键，schema 漏一个字段，那个字段就会在保存后从文件里消失、
    下次读取时回落到模型默认值。

    - 现象：在设置页保存一次，``group_chat.reactions_enabled`` 从 false 变回
      true——贴表情被重新打开，而使用者并没有动过这个开关。
    - 原因：schema 的 ``group_chat`` 只声明了 10 个字段里的 6 个；原先的校验
      只覆盖 models.toml 的两个段，别的文件直接跳过。
    - 后果：默认值为 false 的字段（如 ``pokes_enabled``）丢了也看不出来，正好
      和默认值相同；只有默认值为 true 的字段会暴露，因此这类缺口能潜伏很久。

    子模型字段不计入父段：它们在 schema 里以带点的段名（``typing.nudge``）单独
    声明，重复计入会把正确的 schema 判成缺字段。

    :param schema: 已解析的 schema 字典。
    :raises ValueError: 任一段的字段清单与模型不一致。
    """
    documents: Dict[str, type[BaseModel]] = {
        'bot.toml': BotDocument,
        'features.toml': FeatureDocument,
        _ADAPTER_FILE: AdapterDocument,
    }
    for file_item in schema.get('files', []):
        document = documents.get(file_item.get('file', ''))
        if document is None:
            continue
        for section in file_item.get('sections', []):
            key = section.get('key', '')
            model = _section_model(document, key)
            if model is None:
                continue
            expected = {
                name
                for name, field in model.model_fields.items()
                if not (isinstance(field.annotation, type)
                        and issubclass(field.annotation, BaseModel))
            }
            declared = {entry.get('key') for entry in section.get('fields', [])}
            if declared != expected:
                missing = sorted(expected - declared)
                extra = sorted(declared - expected)
                raise ValueError(
                    f'settings_schema.json 的 {file_item["file"]} [{key}] 与配置模型不一致：'
                    f'缺少 {missing}，多出 {extra}。'
                    '设置页按这份清单写盘，缺的字段会在保存后被改回默认值。'
                )


def _require_task_entries_match_config(schema: Dict[str, Any]) -> None:
    """校验 schema 里的任务条目与配置模型的字段完全一致。

    设置页的写盘是 schema 驱动的：``_write_documented_toml`` 只写 ``entries``
    里列出的键。

    - 现象：新增模型槽后在设置页保存一次，那个槽在 models.toml 里的整段消失，
      已经配好的模型候选被清空，而且不报错。
    - 原因：schema 的 entries 是另一份硬编码任务清单，没跟上 ``ModelTaskConfig``。
    - 后果：静默丢配置是最难察觉的一类故障——用户以为自己没保存成功，实际是被
      清掉了。因此这里宁可在启动期直接失败，也不接受两份清单不一致。

    :param schema: 已解析的 schema 字典。
    :raises ValueError: 任务条目与配置模型字段不一致。
    """
    expected = {
        'model_tasks': set(ModelTaskConfig.model_fields),
        'generation': set(GenerationConfig.model_fields),
    }
    for file_item in schema.get('files', []):
        if file_item.get('file') != 'models.toml':
            continue
        for section in file_item.get('sections', []):
            wanted = expected.get(section.get('key', ''))
            if wanted is None:
                continue
            declared = {entry.get('key') for entry in section.get('entries', [])}
            if declared != wanted:
                missing = sorted(wanted - declared)
                extra = sorted(declared - wanted)
                raise ValueError(
                    f'settings_schema.json 的 {section["key"]} 条目与配置模型不一致：'
                    f'缺少 {missing}，多出 {extra}。'
                    '设置页按这份清单写盘，不补齐会导致保存时静默丢掉那些配置段。'
                )


def file_schema(filename: str) -> Dict[str, Any]:
    """取得单个配置文件名对应的 schema 节点。"""
    for item in load_schema()['files']:
        if item.get('file') == filename:
            return item
    raise ValueError(f'settings_schema.json 未声明配置文件：{filename}')


def _adapter_document_with_section(
    document: Dict[str, Any],
    section: str,
) -> Dict[str, Any]:
    """把模型导出的连接段改名为该适配器在磁盘上使用的段名。

    :param document: ``AdapterDocument.model_dump()`` 的结果。
    :param section: 该适配器清单声明的连接段名。
    :return: 段名替换后的新字典；``section`` 本就是 ``napcat`` 时原样返回。
    """
    if section == PROTOCOL_SECTION_FIELD:
        return document
    renamed = {
        key: value for key, value in document.items()
        if key != PROTOCOL_SECTION_FIELD
    }
    renamed[section] = document[PROTOCOL_SECTION_FIELD]
    return renamed


def _restore(targets: Dict[str, Path], originals: Dict[str, bytes]) -> None:
    """用保存前的字节内容覆盖回全部目标文件，避免停留在半新状态。

    :param targets: 文件标识到绝对路径的映射。
    :param originals: 文件标识到保存前字节内容的映射；空内容表示原本不存在，
        跳过不写，以免凭空造出一个空配置。
    """
    for name, content in originals.items():
        if content:
            targets[name].write_bytes(content)


def _schema_with_adapter_path(adapter_path: Path) -> Dict[str, Any]:
    """返回 schema 副本，并把适配器连接配置的真实路径写进它的说明。

    schema 里的 ``adapter.toml`` 是逻辑标识，磁盘上的文件却随当前适配器变化；
    界面上只显示标识会让人以为在编辑一个叫 adapter.toml 的文件。这里在返回给
    前端之前补一句实际路径，改的是哪份文件当场可见。

    :param adapter_path: 当前适配器连接配置的绝对路径。
    :return: 只改动适配器那一项说明的 schema 深拷贝；不修改模块级缓存。
    """
    schema = copy.deepcopy(load_schema())
    for file_item in schema.get('files', []):
        if file_item.get('file') != _ADAPTER_FILE:
            continue
        description = file_item.get('description', '')
        file_item['description'] = f'{description}实际文件：{adapter_path}。'
    return schema


def _adapter_write_schema(section: str) -> Dict[str, Any]:
    """返回把连接段改名为该适配器实际段名的写入用 schema。

    模型字段与 schema 段名统一用 ``napcat``，磁盘段名由适配器清单决定；带注释的
    TOML 由 schema 段名生成表头，因此只在写入这一步改名。

    :param section: 该适配器清单声明的连接段名。
    :return: 适配器文件的 schema 深拷贝，连接段的 key 已替换为 ``section``。
    """
    schema = copy.deepcopy(file_schema(_ADAPTER_FILE))
    for item in schema.get('sections', []):
        if item.get('key') == PROTOCOL_SECTION_FIELD:
            item['key'] = section
    return schema


def _without_inner(document: Dict[str, Any]) -> Dict[str, Any]:
    """去掉文档中的 inner 版本段，只把业务字段交给前端。"""
    return {key: value for key, value in document.items() if key != 'inner'}


def snapshot(directory: Path) -> Dict[str, Any]:
    """读取全部配置文件并组装 schema + values 的 WebUI 快照。

    :param directory: 主体配置目录，同时含当前适配器的声明文件。
    :return: ``schema`` 为字段映射，``values`` 按文件标识保存业务字段。
    :raises OSError/ValueError/ValidationError: 任一文件缺失、版本不符或字段非法。
    副作用：只读配置，密钥以空字符串返回。
    """
    bot = BotDocument.model_validate(
        read_versioned_toml(directory / 'bot.toml', CONFIG_VERSION, _VERSION_HINT)
    )
    features = FeatureDocument.model_validate(
        read_versioned_toml(directory / 'features.toml', CONFIG_VERSION, _VERSION_HINT)
    )
    plugin_dir = read_active_adapter(directory)
    adapter_path = adapter_config_path(plugin_dir)
    adapter = read_section_config(adapter_path, adapter_config_section(plugin_dir))
    models = model_snapshot(directory)
    providers = []
    for item in models['providers']:
        row = dict(item)
        row['api_key'] = ''
        row['apiKeySet'] = bool(str(item.get('api_key', '')).strip())
        providers.append(row)

    values = {
        'bot.toml': _without_inner(bot.model_dump()),
        'features.toml': _without_inner(features.model_dump()),
        _ADAPTER_FILE: _without_inner(adapter.model_dump()),
        'providers.toml': {'api_providers': providers},
        'models.toml': {
            'model_tasks': models['tasks'],
            'generation': models['generation'],
            'models': models['models'],
        },
    }
    return {'schema': _schema_with_adapter_path(adapter_path), 'values': values}

def _toml_inline(value: Any) -> str:
    """把 Python 标量、列表或字典编码为合法的 TOML 内联值。

    :param value: 待编码的值；支持 str/int/float/bool/list/dict。
    :return: 可直接写入 TOML 文件的值文本。
    :raises TypeError: 遇到不支持的嵌套类型。
    """
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(_toml_inline(item) for item in value) + ']'
    if isinstance(value, dict):
        if not value:
            return '{}'
        parts = []
        for key, item in value.items():
            encoded_key = key if isinstance(key, str) and key and all(char.isalnum() or char in ('_', '-') for char in key) else json.dumps(str(key), ensure_ascii=False)
            parts.append(f'{encoded_key} = {_toml_inline(item)}')
        return '{ ' + ', '.join(parts) + ' }'
    raise TypeError(f'不支持的 TOML 值类型：{type(value).__name__}')


def _comment_lines(text: str) -> List[str]:
    """把一段中文说明转换为逐行 ``# `` 注释，便于写入 TOML 源文件。"""
    return ['# ' + line for line in str(text).splitlines()]


def _write_fields(lines: List[str], fields: List[Dict[str, Any]], values: Dict[str, Any]) -> None:
    """按 schema 顺序把一个配置表内的字段写为带注释的 TOML 行。

    注释采用紧邻配置项的行尾风格 ``key = value # 说明``；说明本身含换行时
    才退回键上方的注释块。
    """
    for field in fields:
        key = field.get('key', '')
        if key not in values:
            continue
        value = values[key]
        if value is None:
            continue
        help_text = str(field.get('help', '')).strip()
        line = f'{key} = {_toml_inline(value)}'
        if help_text and '\n' not in help_text:
            lines.append(f'{line} # {help_text}')
        else:
            lines.extend(_comment_lines(help_text))
            lines.append(line)


def _section_header(key: str, label: str, description: str) -> List[str]:
    """构造小节头；说明单行时挂在表头行尾，多行时保留上方注释块。"""
    text = '：'.join(part for part in (label, description) if part)
    if not text:
        return [f'[{key}]']
    if '\n' not in text:
        return [f'[{key}] # {text}']
    return [*_comment_lines(text), f'[{key}]']


def _section_values(document: Dict[str, Any], key: str) -> Dict[str, Any]:
    """按点分路径取出一个配置段的值，支持 ``[a.b]`` 形式的子表。

    :param document: 已 ``model_dump`` 的配置文档。
    :param key: schema 中声明的段键，可含点号表示嵌套。
    :return: 该段的字段映射；路径不存在或不是映射时返回空字典。
    """
    current: Any = document
    for part in key.split('.'):
        if not isinstance(current, dict):
            return {}
        current = current.get(part, {})
    return current if isinstance(current, dict) else {}


def _write_documented_toml(
    path: Path,
    schema: Dict[str, Any],
    document: Dict[str, Any],
    version: str,
) -> None:
    """用 schema 中的标签与字段说明生成一份带完整注释的 TOML 文件。"""
    lines = [
        f'# {schema.get("label", path.name)}：{schema.get("description", "")}',
        '',
        '[inner]',
        f'version = {_toml_inline(version)}',
        '',
    ]
    for section in schema.get('sections', []):
        kind = section.get('kind', 'object')
        key = section.get('key', '')
        label = section.get('label', key)
        description = section.get('description', '')
        fields = section.get('fields', [])
        if kind == 'object':
            lines.extend(_section_header(key, label, description))
            _write_fields(lines, fields, _section_values(document, key))
            lines.append('')
        elif kind == 'map':
            entries = section.get('entries', [])
            for entry in entries:
                entry_key = entry.get('key', '')
                entry_doc = document.get(key, {})
                values = entry_doc.get(entry_key, {}) if isinstance(entry_doc, dict) else {}
                lines.extend(_section_header(
                    f'{key}.{entry_key}',
                    entry.get('label', entry_key),
                    entry.get('description', ''),
                ))
                entry_fields = [
                    field for field in fields
                    if not field.get('only_for_entries')
                    or entry_key in field.get('only_for_entries', [])
                ]
                _write_fields(lines, entry_fields, values if isinstance(values, dict) else {})
                lines.append('')
        elif kind == 'table_list':
            items = document.get(key, [])
            if not isinstance(items, list):
                items = []
            if not items:
                lines.append(f'{key} = []')
                lines.append('')
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                entry_text = f'一个「{label}」条目'
                if '\n' not in entry_text:
                    lines.append(f'[[{key}]] # {entry_text}')
                else:
                    lines.extend(_comment_lines(f'[[{key}]]：{entry_text}'))
                _write_fields(lines, fields, item)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

def save(directory: Path, values: Dict[str, Any]) -> Dict[str, Any]:
    """校验 WebUI 提交的全部配置并原子写回磁盘。

    :param directory: 主体配置目录，同时含当前适配器的声明文件。
    :param values: 按文件标识组织的业务字段字典，结构与 ``snapshot`` 的 values 一致。
    :return: 成功返回 ``{'ok': True, 'detail': ...}``；失败返回错误说明。
    副作用：校验通过后原子替换主体四个 TOML 与当前适配器的连接配置。
    """
    try:
        bot = BotDocument.model_validate({
            'inner': {'version': CONFIG_VERSION},
            **values.get('bot.toml', {}),
        })
        features = FeatureDocument.model_validate({
            'inner': {'version': CONFIG_VERSION},
            **values.get('features.toml', {}),
        })
        adapter = AdapterDocument.model_validate({
            'inner': {'version': NAPCAT_CONFIG_VERSION},
            **values.get(_ADAPTER_FILE, {}),
        })
        plugin_dir = read_active_adapter(directory)
        adapter_path = adapter_config_path(plugin_dir)
        adapter_section = adapter_config_section(plugin_dir)

        existing = model_snapshot(directory)
        providers = _normalize_providers(
            values.get('providers.toml', {}).get('api_providers', []),
            existing,
        )
        models = _normalize_models(values.get('models.toml', {}).get('models', []))
        tasks = _normalize_tasks(values.get('models.toml', {}).get('model_tasks', {}))
        generation = _normalize_generation(values.get('models.toml', {}).get('generation', {}))

        providers_doc = {
            'inner': {'version': CONFIG_VERSION},
            'api_providers': providers,
        }
        models_doc = {
            'inner': {'version': CONFIG_VERSION},
            'model_tasks': tasks,
            'generation': generation,
            'models': models,
        }
        ProviderCatalog.model_validate(providers_doc)
        ModelCatalog.model_validate(models_doc)
    except Exception as exc:
        return {'ok': False, 'detail': f'配置校验失败：{exc}'}

    # 目标按绝对路径记录：适配器连接配置不在配置目录内，回滚必须覆盖到它。
    targets: Dict[str, Path] = {name: directory / name for name in _CONFIG_FILES}
    targets[_ADAPTER_FILE] = adapter_path
    originals: Dict[str, bytes] = {
        name: path.read_bytes() if path.exists() else b''
        for name, path in targets.items()
    }

    try:
        # 适配器配置的临时文件放在它自己的目录下：os.replace 不能跨卷，而配置
        # 目录可由用户改到另一块盘，与 adapters/ 不保证同卷。
        with tempfile.TemporaryDirectory(dir=directory) as tmp_name,                 tempfile.TemporaryDirectory(dir=adapter_path.parent) as adapter_tmp_name:
            tmp = Path(tmp_name)
            adapter_tmp = Path(adapter_tmp_name) / adapter_path.name
            _write_documented_toml(
                tmp / 'bot.toml', file_schema('bot.toml'),
                bot.model_dump(), CONFIG_VERSION,
            )
            _write_documented_toml(
                tmp / 'features.toml', file_schema('features.toml'),
                features.model_dump(), CONFIG_VERSION,
            )
            _write_documented_toml(
                adapter_tmp, _adapter_write_schema(adapter_section),
                _adapter_document_with_section(adapter.model_dump(), adapter_section),
                NAPCAT_CONFIG_VERSION,
            )
            _write_documented_toml(
                tmp / 'providers.toml', file_schema('providers.toml'),
                providers_doc, CONFIG_VERSION,
            )
            _write_documented_toml(
                tmp / 'models.toml', file_schema('models.toml'),
                models_doc, CONFIG_VERSION,
            )
            # 用与启动阶段完全相同的校验链验证整份新配置，再把验证过的文件换入。
            _load_split_config(tmp)
            read_section_config(adapter_tmp, adapter_section)

            sources: Dict[str, Path] = {name: tmp / name for name in _CONFIG_FILES}
            sources[_ADAPTER_FILE] = adapter_tmp
            replaced: List[str] = []
            try:
                for name, target in targets.items():
                    os.replace(sources[name], target)
                    replaced.append(name)
            except Exception:
                # os.replace 失败时用保存前的字节内容恢复，避免停留在半新状态。
                _restore(targets, originals)
                raise
    except Exception as exc:
        _restore(targets, originals)
        return {'ok': False, 'detail': f'完整配置校验失败：{exc}'}

    logger.info('settings_config_saved', files=replaced)
    return {'ok': True, 'detail': '已保存，重启后端后生效'}
