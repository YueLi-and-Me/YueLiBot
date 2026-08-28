"""按声明式 schema 读取并保存五个运行时 TOML 配置文件的 WebUI 工作台。

模块加载 ``settings_schema.json`` 作为字段、类型、中文说明与文件布局的唯一
映射来源；读取时用 Pydantic 模型补齐默认值并隐藏密钥，保存时先写临时目录、
通过完整启动校验后再原子替换原文件。
"""

from __future__ import annotations

from pathlib import Path
from pydantic import BaseModel
from typing import Any, Dict, List

import json
import os
import shutil
import tempfile

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
from src.platforms.napcat.config import NAPCAT_CONFIG_VERSION, NapcatDocument, read_config as read_napcat_config

logger = get_logger(__name__)

_SCHEMA_PATH = Path(__file__).with_name('settings_schema.json')
_VERSION_HINT = '正常情况下 Electron 启动时会自动升级，手工改过的话请对照模板补齐'
_CONFIG_FILES = (
    'bot.toml', 'features.toml', 'napcat.toml', 'providers.toml', 'models.toml',
)
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
        'napcat.toml': NapcatDocument,
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


def _without_inner(document: Dict[str, Any]) -> Dict[str, Any]:
    """去掉文档中的 inner 版本段，只把业务字段交给前端。"""
    return {key: value for key, value in document.items() if key != 'inner'}


def snapshot(directory: Path) -> Dict[str, Any]:
    """读取五个配置文件并组装 schema + values 的 WebUI 快照。

    :param directory: 包含五个 TOML 文件的配置目录。
    :return: ``schema`` 为字段映射，``values`` 按文件名保存业务字段。
    :raises OSError/ValueError/ValidationError: 任一文件缺失、版本不符或字段非法。
    副作用：只读配置，密钥以空字符串返回。
    """
    bot = BotDocument.model_validate(
        read_versioned_toml(directory / 'bot.toml', CONFIG_VERSION, _VERSION_HINT)
    )
    features = FeatureDocument.model_validate(
        read_versioned_toml(directory / 'features.toml', CONFIG_VERSION, _VERSION_HINT)
    )
    napcat = read_napcat_config(directory / 'napcat.toml')
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
        'napcat.toml': _without_inner(napcat.model_dump()),
        'providers.toml': {'api_providers': providers},
        'models.toml': {
            'model_tasks': models['tasks'],
            'generation': models['generation'],
            'models': models['models'],
        },
    }
    return {'schema': load_schema(), 'values': values}

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
    """按 schema 顺序把一个配置表内的字段写为带注释的 TOML 行。"""
    for field in fields:
        key = field.get('key', '')
        if key not in values:
            continue
        value = values[key]
        if value is None:
            continue
        lines.extend(_comment_lines(field.get('help', '')))
        lines.append(f'{key} = {_toml_inline(value)}')
        lines.append('')


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
        lines.extend(_comment_lines(f'{label}：{description}'))
        if kind == 'object':
            lines.append(f'[{key}]')
            _write_fields(lines, fields, _section_values(document, key))
        elif kind == 'map':
            entries = section.get('entries', [])
            for entry in entries:
                entry_key = entry.get('key', '')
                entry_doc = document.get(key, {})
                values = entry_doc.get(entry_key, {}) if isinstance(entry_doc, dict) else {}
                lines.extend(_comment_lines(
                    f'{entry.get("label", entry_key)}：{entry.get("description", "")}'
                ))
                lines.append(f'[{key}.{entry_key}]')
                entry_fields = [
                    field for field in fields
                    if not field.get('only_for_entries')
                    or entry_key in field.get('only_for_entries', [])
                ]
                _write_fields(lines, entry_fields, values if isinstance(values, dict) else {})
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
                lines.extend(_comment_lines(f'[[{key}]]：一个 {label} 条目'))
                lines.append(f'[[{key}]]')
                _write_fields(lines, fields, item)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

def save(directory: Path, values: Dict[str, Any]) -> Dict[str, Any]:
    """校验 WebUI 提交的五个配置并原子写回磁盘。

    :param directory: 配置目录。
    :param values: 按文件名组织的业务字段字典，结构与 ``snapshot`` 的 values 一致。
    :return: 成功返回 ``{'ok': True, 'detail': ...}``；失败返回错误说明。
    副作用：校验通过后原子替换五个 TOML 文件。
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
        napcat = NapcatDocument.model_validate({
            'inner': {'version': NAPCAT_CONFIG_VERSION},
            **values.get('napcat.toml', {}),
        })

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

    originals: Dict[str, bytes] = {}
    for name in _CONFIG_FILES:
        path = directory / name
        originals[name] = path.read_bytes() if path.exists() else b''

    try:
        with tempfile.TemporaryDirectory(dir=directory) as tmp_name:
            tmp = Path(tmp_name)
            _write_documented_toml(
                tmp / 'bot.toml', file_schema('bot.toml'),
                bot.model_dump(), CONFIG_VERSION,
            )
            _write_documented_toml(
                tmp / 'features.toml', file_schema('features.toml'),
                features.model_dump(), CONFIG_VERSION,
            )
            _write_documented_toml(
                tmp / 'napcat.toml', file_schema('napcat.toml'),
                napcat.model_dump(), NAPCAT_CONFIG_VERSION,
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
            read_napcat_config(tmp / 'napcat.toml')

            replaced: List[str] = []
            try:
                for name in _CONFIG_FILES:
                    os.replace(tmp / name, directory / name)
                    replaced.append(name)
            except Exception:
                # os.replace 失败时用启动前的字节内容恢复，避免目录停留在半新状态。
                for name, content in originals.items():
                    if content:
                        (directory / name).write_bytes(content)
                raise
    except Exception as exc:
        for name, content in originals.items():
            if content:
                (directory / name).write_bytes(content)
        return {'ok': False, 'detail': f'完整配置校验失败：{exc}'}

    logger.info('settings_config_saved', files=replaced)
    return {'ok': True, 'detail': '已保存，重启后端后生效'}
