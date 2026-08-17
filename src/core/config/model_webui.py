"""为 WebUI 模型工作台读写 providers.toml / models.toml 并探测上游。

模块把两份配置转换为表单友好的 JSON 快照，保存时先写入临时文件并复用既有
密钥与引用校验，确认通过后再原子替换磁盘文件。连通性与模型列表探测只读
上游接口，不修改配置。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
import json
import re
import shutil
import tempfile

import httpx

from .loader import CONFIG_VERSION, _load_split_config
from .schema import FeatureDocument, ModelCatalog, ProviderCatalog
from .toml_io import read_versioned_toml
from src.core.common.logger import get_logger

logger = get_logger(__name__)

_PROVIDER_FIELDS = (
    'name', 'kind', 'base_url', 'api_key', 'auth_type', 'auth_name',
    'client_type', 'app_id', 'model_list_endpoint', 'default_headers',
    'default_query', 'timeout_ms', 'max_retries', 'retry_interval_ms',
)
_MODEL_FIELDS = (
    'name', 'model_identifier', 'api_provider', 'extra_body',
    'reasoning_parse_mode', 'visual', 'temperature', 'max_tokens',
    'price_in', 'price_out', 'embedding_dim',
)
TASK_NAMES = (
    'chat', 'proactive', 'summary', 'schedule', 'vision', 'expression',
    'tts', 'embedding',
)
GENERATION_TASKS = ('chat', 'proactive', 'summary', 'schedule', 'expression', 'vision')


def snapshot(directory: Path) -> Dict[str, Any]:
    """读取模型相关配置并返回 WebUI 表单快照。

    :param directory: 包含 providers.toml、models.toml 与 features.toml 的配置目录。
    :return: 包含厂商、模型、任务路由、生成参数和视觉开关的可序列化字典。
    :raises OSError, tomllib.TOMLDecodeError, ValueError: 配置文件读取失败时传播。
    """
    providers = read_versioned_toml(
        directory / 'providers.toml', CONFIG_VERSION, '',
    )
    models = read_versioned_toml(directory / 'models.toml', CONFIG_VERSION, '')
    features = read_versioned_toml(directory / 'features.toml', CONFIG_VERSION, '')

    providers_out = []
    for item in providers.get('api_providers', []):
        if not isinstance(item, dict):
            continue
        row = {key: item.get(key, _default_provider()[key]) for key in _PROVIDER_FIELDS}
        row['apiKeySet'] = bool(str(row.get('api_key', '')).strip())
        row['default_headers'] = dict(row.get('default_headers') or {})
        row['default_query'] = dict(row.get('default_query') or {})
        providers_out.append(row)

    models_out = []
    for item in models.get('models', []):
        if not isinstance(item, dict):
            continue
        row = {key: item.get(key, _default_model()[key]) for key in _MODEL_FIELDS}
        row['extra_body'] = dict(row.get('extra_body') or {})
        models_out.append(row)

    task_cfg = models.get('model_tasks', {})
    tasks_out = {}
    for task in TASK_NAMES:
        raw = task_cfg.get(task, {}) if isinstance(task_cfg, dict) else {}
        tasks_out[task] = {
            'model_list': list(raw.get('model_list') or []),
            'selection_strategy': raw.get('selection_strategy', 'sequential'),
            'first_token_timeout_ms': raw.get('first_token_timeout_ms', 30_000),
            'slow_threshold_ms': raw.get('slow_threshold_ms', 8_000),
        }

    generation = models.get('generation', {})
    generation_out = {}
    for task in GENERATION_TASKS:
        raw = generation.get(task, {}) if isinstance(generation, dict) else {}
        generation_out[task] = {
            'temperature': raw.get('temperature', 0.85),
            'max_tokens': raw.get('max_tokens', 0),
        }
        if task == 'proactive':
            generation_out[task]['enabled'] = bool(raw.get('enabled', True))

    vision = features.get('vision', {}) if isinstance(features, dict) else {}
    return {
        'providers': providers_out,
        'models': models_out,
        'tasks': tasks_out,
        'generation': generation_out,
        'vision_enabled': bool(vision.get('enabled', False)),
        'chat_image_enabled': bool(vision.get('chat_image_enabled', False)),
    }


def _write_vision_bool(directory: Path, field: str, enabled: bool) -> None:
    """只更新 features.toml 的 vision 段布尔字段，保留其余内容。"""
    path = directory / 'features.toml'
    text = path.read_text(encoding='utf-8')
    lines = text.splitlines()
    changed = False
    in_vision = False
    field_line = re.compile(rf'\s*{field}\s*=')
    for index, line in enumerate(lines):
        if line.startswith('['):
            in_vision = line.strip() == '[vision]'
            continue
        if in_vision and field_line.match(line):
            lines[index] = f'{field} = {'true' if enabled else 'false'}'
            changed = True
            break
    if not changed:
        insert_at = next(
            (index for index, line in enumerate(lines) if line.strip() == '[vision]'),
            len(lines),
        ) + 1
        lines.insert(insert_at, f'{field} = {'true' if enabled else 'false'}')
    updated = chr(10).join(lines) + chr(10)
    try:
        FeatureDocument.model_validate(
            read_versioned_toml_with_text(updated, 'features.toml'),
        )
    except Exception as exc:
        raise ValueError(f'{field} 校验失败：{exc}') from exc
    path.write_text(updated, encoding='utf-8')


def read_versioned_toml_with_text(text: str, name: str) -> Dict[str, Any]:
    """用文本解析 TOML 并校验 inner 版本。"""
    import io
    import tomllib
    document = tomllib.loads(text)
    version = document.get('inner', {}).get('version')
    if version != CONFIG_VERSION:
        raise ValueError(f'{name} 配置版本不匹配')
    return document


def save(directory: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    """校验并保存 WebUI 提交的模型配置。

    :param directory: 目标配置目录。
    :param payload: 包含 providers / models / tasks / generation 的表单字典。
    :return: 成功时返回 ``{'ok': True}``；校验失败时返回错误文本。
    """
    existing = snapshot(directory)
    providers = _normalize_providers(payload.get('providers', []), existing)
    models = _normalize_models(payload.get('models', []))
    tasks = _normalize_tasks(payload.get('tasks', {}))
    generation = _normalize_generation(payload.get('generation', {}))

    providers_doc = {'inner': {'version': CONFIG_VERSION}, 'api_providers': providers}
    models_doc = {
        'inner': {'version': CONFIG_VERSION},
        'model_tasks': tasks,
        'generation': generation,
        'models': models,
    }
    try:
        ProviderCatalog.model_validate(providers_doc)
        ModelCatalog.model_validate(models_doc)
    except Exception as exc:
        return {'ok': False, 'detail': f'配置校验失败：{exc}'}

    old_providers = (directory / 'providers.toml').read_bytes()
    old_models = (directory / 'models.toml').read_bytes()
    old_features = (directory / 'features.toml').read_bytes()
    vision_enabled = payload.get('vision_enabled')
    chat_image_enabled = payload.get('chat_image_enabled')
    try:
        if isinstance(vision_enabled, bool):
            _write_vision_bool(directory, 'enabled', vision_enabled)
        if isinstance(chat_image_enabled, bool):
            _write_vision_bool(directory, 'chat_image_enabled', chat_image_enabled)
    except Exception as exc:
        return {'ok': False, 'detail': f'视觉开关保存失败：{exc}'}
    providers_path = directory / 'providers.toml'
    models_path = directory / 'models.toml'
    try:
        providers_path.write_text(_dump_providers(providers), encoding='utf-8')
        models_path.write_text(_dump_models(tasks, generation, models), encoding='utf-8')
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            shutil.copy2(providers_path, tmp_dir / 'providers.toml')
            shutil.copy2(models_path, tmp_dir / 'models.toml')
            shutil.copy2(directory / 'bot.toml', tmp_dir / 'bot.toml')
            shutil.copy2(directory / 'features.toml', tmp_dir / 'features.toml')
            _load_split_config(tmp_dir)
    except Exception as exc:
        providers_path.write_bytes(old_providers)
        models_path.write_bytes(old_models)
        (directory / 'features.toml').write_bytes(old_features)
        return {'ok': False, 'detail': f'完整配置校验失败：{exc}'}
    return {'ok': True, 'detail': '已保存，重启后端后生效'}


def _normalize_providers(items: Any, existing: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把表单厂商列表转换为完整配置行，并复用留空的旧 API Key。"""
    old_by_name = {item['name']: item for item in existing['providers']}
    result: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        row = _default_provider()
        for key in _PROVIDER_FIELDS:
            if key in item:
                row[key] = item[key]
        row['name'] = str(row.get('name') or '').strip()
        if str(row.get('auth_type') or 'bearer') == 'none':
            row['api_key'] = ''
        elif not str(row.get('api_key') or '').strip() and row['name'] in old_by_name:
            old_key = old_by_name[row['name']].get('api_key', '')
            row['api_key'] = str(old_key or '')
        row['default_headers'] = dict(row.get('default_headers') or {})
        row['default_query'] = dict(row.get('default_query') or {})
        row['timeout_ms'] = int(row.get('timeout_ms') or 120_000)
        row['max_retries'] = int(row.get('max_retries') or 2)
        row['retry_interval_ms'] = int(row.get('retry_interval_ms') or 800)
        result.append(row)
    return result


def _normalize_models(items: Any) -> List[Dict[str, Any]]:
    """把表单模型列表转换为完整模型行。"""
    result: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        row = _default_model()
        for key in _MODEL_FIELDS:
            if key in item:
                row[key] = item[key]
        row['name'] = str(row.get('name') or '').strip()
        row['model_identifier'] = str(row.get('model_identifier') or '').strip()
        row['api_provider'] = str(row.get('api_provider') or '').strip()
        row['extra_body'] = dict(row.get('extra_body') or {})
        if row.get('temperature') is None or str(row.get('temperature', '')).strip() == '':
            row['temperature'] = None
        else:
            row['temperature'] = float(row['temperature'])
        if row.get('max_tokens') is None or str(row.get('max_tokens', '')).strip() == '':
            row['max_tokens'] = None
        else:
            row['max_tokens'] = int(row['max_tokens'])
        row['price_in'] = float(row.get('price_in') or 0.0)
        row['price_out'] = float(row.get('price_out') or 0.0)
        result.append(row)
    return result


def _normalize_tasks(items: Any) -> Dict[str, Dict[str, Any]]:
    """把表单任务路由转换为 models.toml 的任务段。"""
    result: Dict[str, Dict[str, Any]] = {}
    for task in TASK_NAMES:
        raw = items.get(task, {}) if isinstance(items, dict) else {}
        model_list = list(raw.get('model_list') or [])
        result[task] = {
            'model_list': [str(name).strip() for name in model_list if str(name).strip()],
            'selection_strategy': raw.get('selection_strategy') or 'sequential',
            'first_token_timeout_ms': int(raw.get('first_token_timeout_ms') or 30_000),
            'slow_threshold_ms': int(raw.get('slow_threshold_ms') or 8_000),
        }
    return result


def _normalize_generation(items: Any) -> Dict[str, Dict[str, Any]]:
    """把表单生成参数转换为 models.toml 的 generation 段。"""
    result: Dict[str, Dict[str, Any]] = {}
    for task in GENERATION_TASKS:
        raw = items.get(task, {}) if isinstance(items, dict) else {}
        result[task] = {
            'temperature': float(raw.get('temperature', 0.85)),
            'max_tokens': int(raw.get('max_tokens') or 0),
        }
        if task == 'proactive':
            result[task]['enabled'] = bool(raw.get('enabled', True))
    return result


def _default_provider() -> Dict[str, Any]:
    return {
        'name': '', 'kind': 'openai', 'base_url': '', 'api_key': '',
        'auth_type': 'bearer', 'auth_name': '', 'client_type': 'openai',
        'app_id': '', 'model_list_endpoint': '/models',
        'default_headers': {}, 'default_query': {},
        'timeout_ms': 120_000, 'max_retries': 2, 'retry_interval_ms': 800,
    }


def _default_model() -> Dict[str, Any]:
    return {
        'name': '', 'model_identifier': '', 'api_provider': '',
        'extra_body': {}, 'reasoning_parse_mode': 'field', 'visual': False,
        'temperature': None, 'max_tokens': None,
        'price_in': 0.0, 'price_out': 0.0, 'embedding_dim': 0,
    }


def _dump_providers(providers: List[Dict[str, Any]]) -> str:
    lines = [
        '# API 厂商与连接策略（WebUI 模型工作台生成）',
        '',
    ]
    if not providers:
        lines.append('api_providers = []')
        lines.append('')
    lines.extend([
        '[inner]',
        f'version = {_toml_value(CONFIG_VERSION)}',
        '',
    ])
    for provider in providers:
        lines.append('[[api_providers]]')
        lines.append(f'name = {_toml_value(provider["name"])}')
        lines.append(f'kind = {_toml_value(provider.get("kind", "openai"))}')
        lines.append(f'base_url = {_toml_value(provider.get("base_url", ""))}')
        lines.append(f'api_key = {_toml_value(provider.get("api_key", ""))}')
        lines.append(f'auth_type = {_toml_value(provider.get("auth_type", "bearer"))}')
        lines.append(f'auth_name = {_toml_value(provider.get("auth_name", ""))}')
        lines.append(f'client_type = {_toml_value(provider.get("client_type", "openai"))}')
        lines.append(f'app_id = {_toml_value(provider.get("app_id", ""))}')
        lines.append(f'model_list_endpoint = {_toml_value(provider.get("model_list_endpoint", "/models"))}')
        lines.append(f'default_headers = {_toml_value(provider.get("default_headers", {}))}')
        lines.append(f'default_query = {_toml_value(provider.get("default_query", {}))}')
        lines.append(f'timeout_ms = {int(provider.get("timeout_ms", 120_000))}')
        lines.append(f'max_retries = {int(provider.get("max_retries", 2))}')
        lines.append(f'retry_interval_ms = {int(provider.get("retry_interval_ms", 800))}')
        lines.append('')
    return '\n'.join(lines)


def _dump_models(
    tasks: Dict[str, Dict[str, Any]],
    generation: Dict[str, Dict[str, Any]],
    models: List[Dict[str, Any]],
) -> str:
    lines = [
        '# 模型定义、任务路由与生成参数（WebUI 模型工作台生成）',
        '',
    ]
    if not models:
        lines.append('models = []')
        lines.append('')
    lines.extend([
        '[inner]',
        f'version = {_toml_value(CONFIG_VERSION)}',
        '',
    ])
    for task in TASK_NAMES:
        raw = tasks[task]
        lines.append(f'[model_tasks.{task}]')
        lines.append(f'model_list = {_toml_value(raw["model_list"])}')
        lines.append(f'selection_strategy = {_toml_value(raw["selection_strategy"])}')
        lines.append(f'first_token_timeout_ms = {int(raw["first_token_timeout_ms"])}')
        lines.append(f'slow_threshold_ms = {int(raw["slow_threshold_ms"])}')
        lines.append('')
    for task in GENERATION_TASKS:
        raw = generation[task]
        lines.append(f'[generation.{task}]')
        if task == 'proactive':
            lines.append(f'enabled = {_toml_value(bool(raw.get("enabled", True)))}')
        lines.append(f'temperature = {_toml_value(float(raw["temperature"]))}')
        lines.append(f'max_tokens = {int(raw["max_tokens"] or 0)}')
        lines.append('')
    for model in models:
        lines.append('[[models]]')
        lines.append(f'name = {_toml_value(model["name"])}')
        lines.append(f'model_identifier = {_toml_value(model.get("model_identifier", ""))}')
        lines.append(f'api_provider = {_toml_value(model.get("api_provider", ""))}')
        lines.append(f'extra_body = {_toml_value(model.get("extra_body", {}))}')
        lines.append(f'reasoning_parse_mode = {_toml_value(model.get("reasoning_parse_mode", "field"))}')
        lines.append(f'visual = {_toml_value(bool(model.get("visual", False)))}')
        if model.get('temperature') is not None:
            lines.append(f'temperature = {_toml_value(float(model["temperature"]))}')
        if model.get('max_tokens') is not None:
            lines.append(f'max_tokens = {int(model["max_tokens"])}')
        lines.append(f'price_in = {_toml_value(float(model.get("price_in") or 0.0))}')
        lines.append(f'price_out = {_toml_value(float(model.get("price_out") or 0.0))}')
        lines.append(f'embedding_dim = {int(model.get("embedding_dim", 0))}')
        lines.append('')
    return '\n'.join(lines)


def _toml_value(value: Any) -> str:
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
        return '[' + ', '.join(_toml_value(item) for item in value) + ']'
    if isinstance(value, dict):
        if not value:
            return '{}'
        parts = []
        for key, item in value.items():
            encoded_key = (
                key
                if isinstance(key, str) and key and all(
                    char.isalnum() or char in ('_', '-') for char in key
                )
                else json.dumps(str(key), ensure_ascii=False)
            )
            parts.append(f'{encoded_key} = {_toml_value(item)}')
        return '{ ' + ', '.join(parts) + ' }'
    return json.dumps(str(value), ensure_ascii=False)


def normalize_base_url(value: str) -> str:
    """规范化厂商基地址，去除尾部斜杠。"""
    return str(value or '').strip().rstrip('/')


async def test_connection(
    base_url: str,
    api_key: str,
    client_type: str,
    auth_type: str,
    auth_name: str,
    model_list_endpoint: str,
    default_headers: Dict[str, str] | None = None,
    default_query: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """测试厂商网络连通性，并在提供 Key 时验证模型列表端点。"""
    normalized = normalize_base_url(base_url)
    result: Dict[str, Any] = {
        'network_ok': False,
        'api_key_valid': None,
        'latency_ms': None,
        'http_status': None,
        'error': None,
    }
    if not normalized:
        result['error'] = 'base_url 不能为空'
        return result
    start = __import__('time').time()
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(normalized)
            result['network_ok'] = True
            result['http_status'] = response.status_code
            result['latency_ms'] = round((__import__('time').time() - start) * 1000, 2)
    except httpx.TimeoutException:
        result['error'] = '连接超时'
        return result
    except Exception as exc:
        result['error'] = f'连接失败：{exc}'
        return result

    if not api_key.strip():
        return result
    try:
        headers = dict(default_headers or {})
        params = dict(default_query or {})
        _apply_auth(headers, params, api_key, auth_type, auth_name, client_type)
        endpoint = str(model_list_endpoint or '/models')
        if not endpoint.startswith('/'):
            endpoint = '/' + endpoint
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(f'{normalized}{endpoint}', headers=headers, params=params)
        if response.status_code == 200:
            result['api_key_valid'] = True
        elif response.status_code in (401, 403):
            result['api_key_valid'] = False
            result['error'] = 'API Key 无效或没有模型列表权限'
        else:
            result['api_key_valid'] = None
            result['error'] = f'模型列表端点返回 HTTP {response.status_code}'
    except Exception as exc:
        logger.warning('provider_model_list_probe_failed', error=str(exc))
        result['api_key_valid'] = None
    return result


async def list_models(
    base_url: str,
    api_key: str,
    client_type: str,
    auth_type: str,
    auth_name: str,
    model_list_endpoint: str,
    default_headers: Dict[str, str] | None = None,
    default_query: Dict[str, str] | None = None,
) -> List[Dict[str, str]]:
    """从 OpenAI 兼容或 Gemini 风格端点拉取可用模型列表。"""
    normalized = normalize_base_url(base_url)
    if not normalized:
        return []
    headers = dict(default_headers or {})
    params = dict(default_query or {})
    _apply_auth(headers, params, api_key, auth_type, auth_name, client_type)
    endpoint = str(model_list_endpoint or '/models')
    if not endpoint.startswith('/'):
        endpoint = '/' + endpoint
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        response = await client.get(f'{normalized}{endpoint}', headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
    if client_type == 'gemini':
        rows = data.get('models', []) if isinstance(data, dict) else []
        return [
            {
                'id': str(row['name'])[7:] if str(row.get('name', '')).startswith('models/') else str(row.get('name', '')),
                'name': str(row.get('displayName') or row.get('name') or ''),
            }
            for row in rows
            if isinstance(row, dict) and row.get('name')
        ]
    rows = data.get('data', []) if isinstance(data, dict) else []
    return [
        {'id': str(row['id']), 'name': str(row.get('name') or row['id'])}
        for row in rows
        if isinstance(row, dict) and row.get('id')
    ]


def _apply_auth(
    headers: Dict[str, str],
    params: Dict[str, str],
    api_key: str,
    auth_type: str,
    auth_name: str,
    client_type: str,
) -> None:
    """按配置给模型列表探测请求附加鉴权。"""
    if client_type == 'gemini':
        params['key'] = api_key
        return
    auth_type = str(auth_type or 'bearer')
    if auth_type == 'bearer' and api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    elif auth_type == 'header' and auth_name and api_key:
        headers[auth_name] = api_key
    elif auth_type == 'query' and auth_name and api_key:
        params[auth_name] = api_key
