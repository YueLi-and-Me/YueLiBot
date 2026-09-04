"""在配置文件缺失时按 schema 默认值生成一份可编辑的初始配置。

配置文件此前只有 Electron 会写：首次启动弹设置窗口，用户填完再落盘。入口反转之后
桌宠可以整个不在场（``[desktop_pet] enabled = false``），于是一份全新签出在无头形态下
根本起不来——``config/`` 不入版本库，没有任何进程去创建它。本模块补上这一段。

生成的内容不是第二份默认值表：除少数「必填且没有合理默认」的字段之外，全部取自
``src.core.config.schema`` 的 Pydantic 默认值，与配置对账脚本认定的真相源同一处；
渲染复用 ``settings_webui`` 的带注释 TOML 写入器，不另写一个序列化器。

产出是**故意不完整的**：模型 ID 与 API Key 只能由人填，写出来的 providers.toml 连
自身的 schema 校验都过不了（``auth_type = bearer`` 要求 api_key 非空）。因此调用方在
创建了主体配置之后应当停下来提示用户，而不是带着一份填不满的配置继续启动。

被 ``src.main`` 在加载配置之前调用；与 ``upgrade`` 的分工是「文件不存在时创建整份」
对「文件已存在时补齐新增字段」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

import tomllib

from .adapter_selection import (
    ADAPTER_SELECTION_FIELD,
    ADAPTER_SELECTION_FILENAME,
    DEFAULT_ADAPTER_PLUGIN,
    adapter_config_path,
    adapter_config_section,
    read_active_adapter,
)
from .loader import CONFIG_VERSION
from .schema import (
    ApiProviderConfig,
    BotDocument,
    FeatureDocument,
    ModelCatalog,
    ModelDefinitionConfig,
    ProviderCatalog,
)
from .settings_webui import (
    _adapter_document_with_section,
    _adapter_write_schema,
    _write_documented_toml,
    file_schema,
)
from src.core.common.logger import get_logger
from src.platforms.onebot11.config import AdapterDocument, NAPCAT_CONFIG_VERSION

logger = get_logger(__name__)

# 主体配置目录内的四个文件；创建了其中任何一个都意味着本次是首次安装。
MAIN_CONFIG_FILES = ('bot.toml', 'features.toml', 'providers.toml', 'models.toml')

# 新装时唯一一条模型连接与它绑定的模型条目名。两个名字要一起改：模型条目通过
# api_provider 指向厂商名，改一处会留下悬空引用，加载期直接报错。
_SEED_PROVIDER_NAME = '主力'
_SEED_MODEL_NAME = 'chat'


def _default_value(model: type[BaseModel], name: str, seed: Dict[str, Any]) -> Any:
    """求出一个字段在初始配置中应当写入的值。

    :param model: 字段所属的模型类。
    :param name: 字段名。
    :param seed: 该模型的种子值；只需覆盖没有默认值的字段。
    :return: 种子值、递归展开的子模型默认值，或字段自身的默认值。
    :raises KeyError: 字段既没有默认值也没有种子值。这不是运行期故障而是维护期
        遗漏：新增了一个必填字段却没在本模块声明初值，会让所有新用户在第一次
        启动时撞上它，因此由 ``pytests`` 提前拦下。
    """
    field = model.model_fields[name]
    annotation = field.annotation
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        nested = seed.get(name, {})
        return _default_document(annotation, nested if isinstance(nested, dict) else {})
    if name in seed:
        return seed[name]
    if field.default_factory is not None:
        return field.default_factory()
    if field.default is not PydanticUndefined:
        return field.default
    raise KeyError(
        f'{model.__name__}.{name} 必填且没有默认值，'
        f'需要在 src/core/config/bootstrap.py 的种子表里给出初值'
    )


def _default_document(model: type[BaseModel], seed: Dict[str, Any]) -> Dict[str, Any]:
    """按字段顺序展开一个模型的初始值字典。

    不走 ``model_validate``：初始配置里 api_key 与模型 ID 必然为空，而 schema 会
    因此拒绝整份文档。写入器接受普通字典，用不着先构造出一个合法模型。

    :param model: 待展开的模型类。
    :param seed: 该模型的种子值，键可以是标量、列表或子模型的嵌套字典。
    :return: 可直接交给带注释 TOML 写入器的字典。
    :raises KeyError: 存在既无默认值又无种子值的字段。
    """
    return {name: _default_value(model, name, seed) for name in model.model_fields}


def _seed_provider() -> Dict[str, Any]:
    """给出新装时唯一一条模型连接的初值。"""
    return _default_document(ApiProviderConfig, {
        'name': _SEED_PROVIDER_NAME,
        # kind 只是给人看的分类标签，填哪个都不影响请求；这里取最常见的一种。
        'kind': 'ark',
    })


def _seed_model() -> Dict[str, Any]:
    """给出新装时唯一一条模型条目的初值。

    ``model_identifier`` 留空：具体模型 ID 因厂商而异，猜一个只会让用户以为已经
    配好，直到第一次对话才发现请求被拒。
    """
    return _default_document(ModelDefinitionConfig, {
        'name': _SEED_MODEL_NAME,
        'api_provider': _SEED_PROVIDER_NAME,
    })


def _bot_document() -> Dict[str, Any]:
    """组装 bot.toml 的初始文档。

    人格与称呼全部留空：它们是这个项目最个人化的部分，预填一份别人的设定，
    用户多半不会逐条清空，而是带着它跑起来。
    """
    return _default_document(BotDocument, {
        'inner': {'version': CONFIG_VERSION},
        'bot': {'name': ''},
        # 该字段要求显式配置（不接受缺省），初值取「@ 必回」——群里被点名不理人
        # 比多回一句更容易被当成故障。
        'group_chat': {'at_mention_must_reply': True},
        'personality': {
            'birthday': '',
            'personality': '',
            'reply_style': '',
            'tone_probability': 0.0,
            'tone_variants': [],
        },
    })


def _feature_document() -> Dict[str, Any]:
    """组装 features.toml 的初始文档；功能开关全部取 schema 默认值。"""
    return _default_document(FeatureDocument, {'inner': {'version': CONFIG_VERSION}})


def _provider_document() -> Dict[str, Any]:
    """组装 providers.toml 的初始文档，含一条待填写的连接。"""
    return _default_document(ProviderCatalog, {
        'inner': {'version': CONFIG_VERSION},
        'api_providers': [_seed_provider()],
    })


def _model_document() -> Dict[str, Any]:
    """组装 models.toml 的初始文档。

    只把 chat 任务指向那条种子模型，其余任务的候选留空：任务没有候选时对应功能
    直接不启用，比指向一个没配好的模型更容易排查。
    """
    document = _default_document(ModelCatalog, {
        'inner': {'version': CONFIG_VERSION},
        'models': [_seed_model()],
    })
    document['model_tasks']['chat']['model_list'] = [_SEED_MODEL_NAME]
    return document


def _adapter_document() -> Dict[str, Any]:
    """组装 QQ 适配器连接配置的初始文档。

    ``enabled`` 取 false：连接协议端会用真实 QQ 号登录，不能在用户确认之前发生。
    """
    return _default_document(AdapterDocument, {
        'inner': {'version': NAPCAT_CONFIG_VERSION},
        'napcat': {
            'enabled': False,
            'host': '127.0.0.1',
            'port': 8095,
            'token': '',
            'reconnect_interval_sec': 5.0,
            'action_timeout_sec': 15.0,
        },
    })


def _ensure_adapter_selection(config_dir: Path) -> tuple[str, bool]:
    """读取当前启用的适配器插件目录名，声明文件缺失时按默认值创建。

    这里选一个默认适配器不违反 ``adapter_selection`` 的「读取时不回退」原则：
    创建是一次显式的初始化，用户随后可以改；读取时回退才是把「配错了」伪装成
    「配好了」。

    :param config_dir: 主体配置目录；不存在时递归创建。
    :return: ``(插件目录名, 是否本次创建)``。
    :raises OSError: 目录或文件无法写入。
    :raises ValueError: 声明文件已存在但缺少非空的插件目录名。
    副作用：可能创建配置目录与声明文件。
    """
    path = config_dir / ADAPTER_SELECTION_FILENAME
    if path.is_file():
        return read_active_adapter(config_dir), False
    config_dir.mkdir(parents=True, exist_ok=True)
    # 这份声明只有一个字段，不套 [inner].version：它不随配置结构演进，
    # 加一层版本壳只会多一处需要同步升级的地方。
    path.write_text('\n'.join([
        '# 当前启用的 QQ 适配器：adapters/ 下的插件目录名。',
        '# 两个协议端后端互斥，同时只能开一个；桌宠与主体都读这一处声明。',
        f'{ADAPTER_SELECTION_FIELD} = "{DEFAULT_ADAPTER_PLUGIN}"',
        '',
    ]), encoding='utf-8')
    return DEFAULT_ADAPTER_PLUGIN, True


def bootstrap_config_directory(config_dir: Path) -> List[Path]:
    """补齐缺失的配置文件，返回本次创建的文件路径。

    只创建不存在的文件，已有文件一律不动：本函数每次启动都会跑，覆盖已有配置
    等于把用户填好的密钥冲掉。

    :param config_dir: 主体配置目录；不存在时递归创建。
    :return: 本次创建的文件路径列表，按创建顺序排列；全部已存在时为空列表。
    :raises KeyError: schema 新增了必填字段但本模块没有给出初值。
    :raises OSError: 目录或文件无法写入。
    :raises ValueError: 适配器声明文件存在但内容非法，或它指向的插件目录不存在。
    副作用：创建配置目录与缺失的配置文件，并为每个新建文件输出一行日志。
    """
    created: List[Path] = []
    plugin_dir, selection_created = _ensure_adapter_selection(config_dir)
    if selection_created:
        created.append(config_dir / ADAPTER_SELECTION_FILENAME)

    documents = {
        'bot.toml': _bot_document(),
        'features.toml': _feature_document(),
        'providers.toml': _provider_document(),
        'models.toml': _model_document(),
    }
    for name in MAIN_CONFIG_FILES:
        path = config_dir / name
        if path.is_file():
            continue
        _write_documented_toml(path, file_schema(name), documents[name], CONFIG_VERSION)
        created.append(path)

    adapter_path = adapter_config_path(plugin_dir)
    if not adapter_path.is_file():
        section = adapter_config_section(plugin_dir)
        _write_documented_toml(
            adapter_path,
            _adapter_write_schema(section),
            _adapter_document_with_section(_adapter_document(), section),
            NAPCAT_CONFIG_VERSION,
        )
        created.append(adapter_path)

    for path in created:
        logger.info('config_file_created', path=str(path))
    return created


def missing_startup_requirements(config_dir: Path) -> List[str]:
    """列出配置里还缺哪些「必须由人填」的东西。

    只看能否启动一轮对话，不重复 schema 的结构校验：结构错误由加载器完整报出，
    而「格式都对、就是还没填」需要一句人话，否则用户看到的是一串字段路径。

    :param config_dir: 主体配置目录。
    :return: 面向用户的缺项说明，文件名相对配置目录书写；配置已可启动时为空列表。
    :raises OSError: 配置文件无法读取。
    :raises tomllib.TOMLDecodeError: 配置文件不是合法 TOML。
    副作用：只读取配置文件。
    """
    missing: List[str] = []
    bot = tomllib.loads((config_dir / 'bot.toml').read_text(encoding='utf-8'))
    if not str(bot.get('bot', {}).get('name', '')).strip():
        missing.append('bot.toml 的 [bot] name：她叫什么')

    models = tomllib.loads((config_dir / 'models.toml').read_text(encoding='utf-8'))
    providers = tomllib.loads((config_dir / 'providers.toml').read_text(encoding='utf-8'))
    catalog = {
        str(item.get('name', '')): item
        for item in models.get('models', []) if isinstance(item, dict)
    }
    connections = {
        str(item.get('name', '')): item
        for item in providers.get('api_providers', []) if isinstance(item, dict)
    }
    candidates = models.get('model_tasks', {}).get('chat', {}).get('model_list', [])
    usable = False
    for name in candidates if isinstance(candidates, list) else []:
        model = catalog.get(str(name))
        if not model or not str(model.get('model_identifier', '')).strip():
            continue
        connection = connections.get(str(model.get('api_provider', '')))
        if connection is None:
            continue
        # auth_type = none 的连接（本地推理服务）必须留空 api_key，拿密钥非空当判据
        # 会把一份完全正确的本地配置报成「没填完」。
        if str(connection.get('auth_type', 'bearer')) == 'none':
            usable = True
            break
        if str(connection.get('api_key', '')).strip():
            usable = True
            break
    if not usable:
        missing.append(
            'models.toml 的 [[models]] model_identifier，'
            '以及 providers.toml 的 [[api_providers]] base_url、api_key：'
            '对话任务至少要有一条填好的模型连接'
        )
    return missing


__all__ = [
    'MAIN_CONFIG_FILES',
    'bootstrap_config_directory',
    'missing_startup_requirements',
]
