"""为 WebUI 模型工作台读写 providers.toml / models.toml 并探测上游。

模块把两份配置转换为表单友好的 JSON 快照，保存时先写入临时文件并复用既有
密钥与引用校验，确认通过后再原子替换磁盘文件。连通性与模型列表探测只读
上游接口，不修改配置。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List
import json
import re
import shutil
import tempfile

import httpx

from .loader import CONFIG_VERSION, _load_split_config
from .schema import (
    FeatureDocument,
    GenerationConfig,
    ModelCatalog,
    ModelTaskConfig,
    ProviderCatalog,
)
from .toml_io import read_versioned_toml
from src.core.logging.logger import get_logger

logger = get_logger(__name__)

# 表单允许提交的厂商行字段集合，与 _default_provider 的键一致。
_PROVIDER_FIELDS = (
    'name', 'kind', 'base_url', 'api_key', 'auth_type', 'auth_name',
    'client_type', 'app_id', 'model_list_endpoint', 'default_headers',
    'default_query', 'timeout_ms', 'max_retries', 'retry_interval_ms',
)
# 表单允许提交的模型行字段集合，与 _default_model 的键一致。
_MODEL_FIELDS = (
    'name', 'model_identifier', 'api_provider', 'extra_body',
    'api_format', 'reasoning_parse_mode', 'visual', 'omni', 'temperature', 'max_tokens',
    'embedding_dim', 'price_in', 'price_out', 'cache_price_in', 'price_periods',
)
# 价格段（price_in 起到该模型的最后一个 [[models.price_periods]] 止）的行尾注释。
# Electron 写入器 electron/main/config.ts 的 PRICE_COMMENTS 与 settings_schema.json 的
# 字段说明必须与这里逐字相同：三处写出的价格段因此逐字一致，用例对照两个写入器的输出。
PRICE_COMMENTS: Dict[str, str] = {
    'price_in': '输入单价，单位元/百万 token；用于用量统计计算费用，0 表示未填写或免费。',
    'price_out': '输出单价（含思考），单位元/百万 token；用于用量统计计算费用，0 表示未填写或免费。',
    'cache_price_in': '缓存命中部分的输入单价，单位元/百万 token；不写表示与 price_in 相同，0 表示缓存命中免费。',
    'price_periods': '分时段单价：按本机时区，含开始、不含结束；结束早于开始表示跨午夜；不在任何时段时用上面的基础单价。',
    'start': '时段开始，HH:MM，包含该时刻。',
    'end': '时段结束，HH:MM，不包含该时刻；早于开始表示跨午夜。',
    'period_price_in': '该时段的输入单价，单位元/百万 token。',
    'period_price_out': '该时段的输出单价（含思考），单位元/百万 token。',
    'period_cache_price_in': '该时段缓存命中部分的输入单价；不写表示与该时段 price_in 相同，0 表示免费。',
}
# 任务清单从配置模型派生，不在这里再抄一份。
#
# - 现象：新增 planner / replyer 两个模型槽后，WebUI 能选到它们，保存却不生效。
# - 原因：本模块原来硬编码了一份任务名，`_normalize_tasks` 只认这份清单，
#   槽名不在其中的表单项会被静默丢弃，落盘的 models.toml 里根本没有那两段。
# - 后果：任何新增模型槽都必须同时改这里，漏改的表现是「界面上能配、重启后没变化」，
#   而且不报错。改成从 schema 派生之后，加槽只需要动 schema 一处。
TASK_NAMES = tuple(ModelTaskConfig.model_fields)
GENERATION_TASKS = tuple(GenerationConfig.model_fields)


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
        'chat_video_enabled': bool(vision.get('chat_video_enabled', False)),
    }


def _write_vision_bool(directory: Path, field: str, enabled: bool) -> None:
    """只更新 features.toml 的 vision 段布尔字段，保留其余内容。

    段头与字段行都可能带行尾注释，比较前先剥掉。

    :param directory: 目标配置目录。
    :param field: ``[vision]`` 段内的布尔字段名。
    :param enabled: 字段写入 ``true`` 还是 ``false``。
    :return: 无返回值。
    :raises ValueError: 更新后的文本不能通过 ``FeatureDocument`` 校验。
    副作用：就地重写 ``features.toml``；字段不存在时插入到段头之后。
    """
    path = directory / 'features.toml'
    text = path.read_text(encoding='utf-8')
    lines = text.splitlines()
    changed = False
    in_vision = False
    field_line = re.compile(rf'\s*{field}\s*=')
    for index, line in enumerate(lines):
        if line.startswith('['):
            in_vision = line.split('#', 1)[0].strip() == '[vision]'
            continue
        if in_vision and field_line.match(line):
            lines[index] = f'{field} = {'true' if enabled else 'false'}'
            changed = True
            break
    if not changed:
        insert_at = next(
            (index for index, line in enumerate(lines) if line.split('#', 1)[0].strip() == '[vision]'),
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
    """用文本解析 TOML 并校验 inner 版本。

    :param text: 待解析的完整 TOML 文本。
    :param name: 配置文件名，仅用于错误消息定位。
    :return: 解析后的 TOML 文档。
    :raises ValueError: ``inner.version`` 与 :data:`CONFIG_VERSION` 不一致。
    副作用：无。
    """
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
    chat_video_enabled = payload.get('chat_video_enabled')
    try:
        if isinstance(vision_enabled, bool):
            _write_vision_bool(directory, 'enabled', vision_enabled)
        if isinstance(chat_image_enabled, bool):
            _write_vision_bool(directory, 'chat_image_enabled', chat_image_enabled)
        if isinstance(chat_video_enabled, bool):
            _write_vision_bool(directory, 'chat_video_enabled', chat_video_enabled)
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
    """把表单厂商列表转换为完整配置行，并复用留空的旧 API Key。

    :param items: WebUI 提交的厂商行列表；非字典项跳过。
    :param existing: 保存前的配置快照，用于按名称取旧密钥。
    :return: 补齐默认值后的厂商配置行；留空密钥且旧配置有同名厂商时沿用
        旧密钥，``auth_type=none`` 时密钥强制清空。
    副作用：无。
    """
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
    """把表单模型列表转换为完整模型行。

    :param items: WebUI 提交的模型行列表；非字典项跳过。
    :return: 补齐默认值、字符串去空白、数值字段定型后的模型配置行；
        空白 ``temperature``/``max_tokens`` 归一为 ``None``（不覆盖）。
    副作用：无。
    """
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
        row['cache_price_in'] = _optional_price(row.get('cache_price_in'))
        row['price_periods'] = [
            {
                'start': str(period.get('start') or '').strip(),
                'end': str(period.get('end') or '').strip(),
                'price_in': float(period.get('price_in') or 0.0),
                'price_out': float(period.get('price_out') or 0.0),
                'cache_price_in': _optional_price(period.get('cache_price_in')),
            }
            for period in row.get('price_periods') or []
            if isinstance(period, dict)
        ]
        result.append(row)
    return result


def _optional_price(value: Any) -> float | None:
    """把可留空的单价表单值规整为浮点数或 ``None``。

    :param value: 表单值；``None`` 与空白字符串表示留空。
    :return: 留空时为 ``None``，否则为浮点数。
    :raises ValueError: 非空值不是数字。
    副作用：无。
    """
    if value is None or str(value).strip() == '':
        return None
    return float(value)


def _normalize_tasks(items: Any) -> Dict[str, Dict[str, Any]]:
    """把表单任务路由转换为 models.toml 的任务段。

    :param items: WebUI 提交的按任务名的路由配置；缺失任务补默认值。
    :return: 覆盖全部 :data:`TASK_NAMES` 的任务路由行。
    副作用：无。
    """
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
    """把表单生成参数转换为 models.toml 的 generation 段。

    :param items: WebUI 提交的按任务名的生成参数；缺失任务补默认值。
    :return: 覆盖全部 :data:`GENERATION_TASKS` 的生成参数行；proactive
        额外携带 ``enabled`` 开关。
    副作用：无。
    """
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
    """构造一个厂商行的完整默认值模板，字段集合须与 :data:`_PROVIDER_FIELDS` 一致。

    :return: 含全部厂商字段的字典，名称与地址为空串。
    """
    return {
        'name': '', 'kind': 'openai', 'base_url': '', 'api_key': '',
        'auth_type': 'bearer', 'auth_name': '', 'client_type': 'openai',
        'app_id': '', 'model_list_endpoint': '/models',
        'default_headers': {}, 'default_query': {},
        'timeout_ms': 120_000, 'max_retries': 2, 'retry_interval_ms': 800,
    }


def _default_model() -> Dict[str, Any]:
    """构造一个模型行的完整默认值模板，字段集合须与 :data:`_MODEL_FIELDS` 一致。

    :return: 含全部模型字段的字典，名称与模型标识为空串，覆盖类参数为 ``None``。
    """
    return {
        'name': '', 'model_identifier': '', 'api_provider': '',
        'extra_body': {}, 'api_format': 'openai', 'reasoning_parse_mode': 'field', 'visual': False,
        'omni': False, 'temperature': None, 'max_tokens': None,
        'embedding_dim': 0, 'price_in': 0.0, 'price_out': 0.0,
        'cache_price_in': None, 'price_periods': [],
    }


def _dump_providers(providers: List[Dict[str, Any]]) -> str:
    """把厂商列表渲染成带字段注释的 providers.toml 文本。

    :param providers: 已归一化的厂商配置行。
    :return: 完整的 TOML 文件文本，首行注明由 WebUI 生成。
    副作用：无。
    """
    lines = [
        '# API 厂商与连接策略；由 WebUI 模型工作台生成，手工改动会在下次保存时被覆盖。',
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
        lines.extend([
            '[[api_providers]] # 一个 API 厂商条目。',
            f'name = {_toml_value(provider["name"])} # 模型配置引用的唯一名称；不能与其他厂商重复。',
            f'kind = {_toml_value(provider.get("kind", "openai"))} # 预设标识，如 deepseek、openai、ark；base_url 为空时按预设取官方地址。',
            f'base_url = {_toml_value(provider.get("base_url", ""))} # API 端点基础 URL，通常以 /v1 结尾；留空使用预设官方地址。',
            f'api_key = {_toml_value(provider.get("api_key", ""))} # 身份验证密钥；编辑时留空表示保持已保存的密钥不变。',
            f'auth_type = {_toml_value(provider.get("auth_type", "bearer"))} # Bearer 使用 Authorization 头；header/query 需要填写 auth_name。',
            f'auth_name = {_toml_value(provider.get("auth_name", ""))} # header/query 鉴权使用的字段名；Bearer/none 时必须留空。',
            f'client_type = {_toml_value(provider.get("client_type", "openai"))} # OpenAI 兼容支持对话、视觉、向量和 TTS；volcengine 只允许绑定 TTS 任务。',
            f'app_id = {_toml_value(provider.get("app_id", ""))} # 豆包语音的服务接口认证信息；仅 client_type=volcengine 时生效。',
            f'model_list_endpoint = {_toml_value(provider.get("model_list_endpoint", "/models"))} # 用于 WebUI 连通性测试与模型拉取的路径；OpenAI 兼容默认 /models。',
            f'default_headers = {_toml_value(provider.get("default_headers", {}))} # 需要额外 HTTP 头的厂商在这里写键值；认证头仍由 auth_type/auth_name 负责。',
            f'default_query = {_toml_value(provider.get("default_query", {}))} # 需要固定查询参数的厂商在这里写键值。',
            f'timeout_ms = {int(provider.get("timeout_ms", 120_000))} # 单次 HTTP 连接与流式读取超时；首字阶段的内部重试仍受任务级首字超时整体截断。',
            f'max_retries = {int(provider.get("max_retries", 2))} # 同一连接内尚未输出内容时的重试次数；需要跑满重试应调大任务级首字超时。',
            f'retry_interval_ms = {int(provider.get("retry_interval_ms", 800))} # 两次重试之间的等待时间。',
            '',
        ])
    return '\n'.join(lines)


def _dump_models(
    tasks: Dict[str, Dict[str, Any]],
    generation: Dict[str, Dict[str, Any]],
    models: List[Dict[str, Any]],
) -> str:
    """把任务路由、生成参数与模型列表渲染成带字段注释的 models.toml 文本。

    :param tasks: 已归一化的按任务名路由配置。
    :param generation: 已归一化的按任务名生成参数。
    :param models: 已归一化的模型配置行。
    :return: 完整的 TOML 文件文本，首行注明由 WebUI 生成。
    副作用：无。
    """
    lines = [
        '# 模型定义、任务路由与生成参数；由 WebUI 模型工作台生成，手工改动会在下次保存时被覆盖。',
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
        lines.extend([
            f'[model_tasks.{task}]',
            f'model_list = {_toml_value(raw["model_list"])} # 该任务使用的候选模型名称，对应下方 [[models]] 的 name；列表顺序只在「顺序优先」策略下决定先后。',
            f'selection_strategy = {_toml_value(raw["selection_strategy"])} # sequential 永远优先第一条；random 每次随机打乱；balance 在健康候选之间逐轮分摊请求。',
            f'first_token_timeout_ms = {int(raw["first_token_timeout_ms"])} # 候选切换窗口；窗口耗尽会直接切换下一个候选模型。',
            f'slow_threshold_ms = {int(raw["slow_threshold_ms"])} # 只用于慢响应记账；必须小于首字超时，0 表示关闭。',
            '',
        ])
    for task in GENERATION_TASKS:
        raw = generation[task]
        lines.append(f'[generation.{task}]')
        if task == 'proactive':
            lines.append(
                f'enabled = {_toml_value(bool(raw.get("enabled", True)))}'
                ' # 关闭后 Electron 不安装全局键鼠钩子，也不会主动发起互动。'
            )
        lines.append(
            f'temperature = {_toml_value(float(raw["temperature"]))}'
            ' # 采样温度；低温度更确定，高温度更有创造性。'
        )
        lines.append(
            f'max_tokens = {int(raw["max_tokens"] or 0)}'
            ' # 最大输出 token 数；0 表示不额外限制，沿用厂商上限。'
        )
        lines.append('')
    for model in models:
        lines.append('[[models]] # 一个模型条目。')
        lines.append(f'name = {_toml_value(model["name"])} # 任务候选引用的唯一名称；不能与其他模型重复。')
        lines.append(
            f'model_identifier = {_toml_value(model.get("model_identifier", ""))}'
            ' # 厂商接口接受的真实模型 ID；留空的模型被任务引用时会在加载期报错。'
        )
        lines.append(f'api_provider = {_toml_value(model.get("api_provider", ""))} # 引用的厂商名称，必须存在于 providers.toml。')
        lines.append(f'extra_body = {_toml_value(model.get("extra_body", {}))} # 透传给厂商请求体的额外参数；JSON 对象格式。')
        lines.append(
            f'api_format = {_toml_value(model.get("api_format", "openai"))}'
            ' # 接口协议：openai 为 OpenAI 兼容，responses 为 OpenAI Responses，'
            'dashscope_multimodal / ark_multimodal 为百炼 / 方舟多模态向量；按模型选择。'
        )
        lines.append(
            f'reasoning_parse_mode = {_toml_value(model.get("reasoning_parse_mode", "field"))}'
            ' # 解析模型思考内容的方式：field 从响应字段读取，tag 从文本标签解析，none 不解析。'
        )
        lines.append(f'visual = {_toml_value(bool(model.get("visual", False)))} # 标记该模型可用于 vision / 图片描述任务。')
        lines.append(f'omni = {_toml_value(bool(model.get("omni", False)))} # 标记该模型是全模态，可用于 video 视频理解任务（同时看画面、听声音）。')
        if model.get('temperature') is not None:
            lines.append(
                f'temperature = {_toml_value(float(model["temperature"]))}'
                ' # 模型级覆盖；留空时使用任务 generation 配置。'
            )
        if model.get('max_tokens') is not None:
            lines.append(
                f'max_tokens = {int(model["max_tokens"])}'
                ' # 模型级覆盖；留空时使用任务 generation 配置。'
            )
        lines.append(f'embedding_dim = {int(model.get("embedding_dim", 0))} # 仅嵌入模型需要填写；同一任务下候选的模型 ID、接口协议与维度必须一致。')
        # 价格段放在模型条目末尾：[[models.price_periods]] 子表之后不能再写本模型的键。
        lines.extend(price_lines(model))
        lines.append('')
    return '\n'.join(lines)


def toml_price(value: float) -> str:
    """把单价写成定点小数，与 Electron 写入器的 ``tomlPrice`` 逐字同口径。

    取最短往返的十进制数字，统一展开成不带指数的写法，整数值补 ``.0``：Python 的
    ``repr`` 在小于 1e-4 时改用指数写法，JavaScript 的阈值是 1e-6，直接各用各的会让
    两个写入器对同一单价写出不同文本。

    :param value: 非负单价。
    :return: TOML 浮点字面量，如 ``2.0``、``0.00005``。
    副作用：无。
    """
    text = format(Decimal(repr(float(value))), 'f')
    return text if '.' in text else f'{text}.0'


def price_lines(model: Dict[str, Any]) -> List[str]:
    """渲染一个模型的价格段：基础单价、缓存命中价与分时段单价子表。

    与 Electron 写入器的 ``priceBlock`` 输出逐字一致；留空的缓存命中价不写键。

    :param model: 已归一化的模型配置行。
    :return: 价格段的 TOML 行，以 ``price_in`` 开头。
    副作用：无。
    """
    lines = [
        f'price_in = {toml_price(model["price_in"])} # {PRICE_COMMENTS["price_in"]}',
        f'price_out = {toml_price(model["price_out"])} # {PRICE_COMMENTS["price_out"]}',
    ]
    if model.get('cache_price_in') is not None:
        lines.append(
            f'cache_price_in = {toml_price(model["cache_price_in"])} # {PRICE_COMMENTS["cache_price_in"]}'
        )
    for period in model.get('price_periods') or []:
        lines.append(f'[[models.price_periods]] # {PRICE_COMMENTS["price_periods"]}')
        lines.append(f'start = {_toml_value(period["start"])} # {PRICE_COMMENTS["start"]}')
        lines.append(f'end = {_toml_value(period["end"])} # {PRICE_COMMENTS["end"]}')
        lines.append(f'price_in = {toml_price(period["price_in"])} # {PRICE_COMMENTS["period_price_in"]}')
        lines.append(f'price_out = {toml_price(period["price_out"])} # {PRICE_COMMENTS["period_price_out"]}')
        if period.get('cache_price_in') is not None:
            lines.append(
                f'cache_price_in = {toml_price(period["cache_price_in"])}'
                f' # {PRICE_COMMENTS["period_cache_price_in"]}'
            )
    return lines


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
    """规范化厂商基地址，去除首尾空白与尾部斜杠。

    :param value: 原始基地址；``None`` 按空串处理。
    :return: 规范化后的基地址，可直接与端点路径拼接。
    副作用：无。
    """
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
    """测试厂商网络连通性，并在提供 Key 时验证模型列表端点。

    两段探测互相独立：连通性只打基地址本身，不携带鉴权；Key 校验打模型
    列表端点。任何失败都不抛异常，错误以文本进入结果字典。

    :param base_url: 厂商 API 基础地址。
    :param api_key: 待验证的 API Key；空串时跳过 Key 校验。
    :param client_type: 客户端协议类型，决定鉴权附加方式。
    :param auth_type: ``bearer``/``header``/``query``/``none``。
    :param auth_name: header/query 鉴权的字段名。
    :param model_list_endpoint: 模型列表端点路径，默认 ``/models``。
    :param default_headers: 额外请求头，可为 ``None``。
    :param default_query: 固定查询参数，可为 ``None``。
    :return: 含 ``network_ok``、``api_key_valid``、``latency_ms``、
        ``http_status``、``error`` 的结果字典；``api_key_valid`` 为 ``None``
        表示未验证或无法判定。
    副作用：发起最多两次 HTTP GET 请求（连通性 10 秒超时、Key 校验 15 秒超时）。
    """
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
    """从 OpenAI 兼容或 Gemini 风格端点拉取可用模型列表。

    :param base_url: 厂商 API 基础地址；空串直接返回空列表。
    :param api_key: 请求使用的 API Key。
    :param client_type: ``openai`` 或 ``gemini``，决定响应结构与模型 ID 提取方式。
    :param auth_type: ``bearer``/``header``/``query``/``none``。
    :param auth_name: header/query 鉴权的字段名。
    :param model_list_endpoint: 模型列表端点路径，默认 ``/models``。
    :param default_headers: 额外请求头，可为 ``None``。
    :param default_query: 固定查询参数，可为 ``None``。
    :return: ``{id, name}`` 行列表；上游 HTTP 错误原样抛出，由路由层转 502。
    :raises httpx.HTTPStatusError: 上游返回非 2xx 状态。
    副作用：发起一次 HTTP GET 请求（30 秒超时）。
    """
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
    """按配置给模型列表探测请求附加鉴权。

    gemini 一律走 query 参数 ``key``；其余按 ``auth_type`` 写 Authorization 头、
    自定义头或自定义查询参数，字段名缺失时不附加。

    :param headers: 就地更新的请求头字典。
    :param params: 就地更新的查询参数字典。
    :param api_key: 待附加的 API Key。
    :param auth_type: ``bearer``/``header``/``query``/``none``。
    :param auth_name: header/query 鉴权的字段名。
    :param client_type: ``openai`` 或 ``gemini``。
    :return: 无返回值，通过入参字典带回结果。
    副作用：修改传入的 ``headers`` 与 ``params``。
    """
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
