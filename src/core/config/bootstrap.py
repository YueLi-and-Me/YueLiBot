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
    ADAPTERS_ROOT,
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
from src.core.logging.logger import get_logger
from src.platforms.onebot11.config import AdapterDocument, NAPCAT_CONFIG_VERSION

logger = get_logger(__name__)

# 主体配置目录内的四个文件；创建了其中任何一个都意味着本次是首次安装。
MAIN_CONFIG_FILES = ('bot.toml', 'features.toml', 'providers.toml', 'models.toml')

# 新装预填的厂商连接。六个模型全部走这一条：DeepSeek 系列也由百炼托管，
# 不需要单独开一个 DeepSeek 账号。模型条目通过 api_provider 引用这个名字，
# 改名字要顺着这条链一起改，否则加载期会因悬空引用直接报错退出。
#
# 只留一条连接是刻意的：schema 对目录里每个厂商都要求非空 api_key，不区分是否
# 被任务引用。多预填一条就等于多逼用户开一个账号，而不是「用不到就放着」。
_DASHSCOPE_PROVIDER = 'dashscope'
DASHSCOPE_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'

# 关掉思考。本项目不设任务级的 thinking 开关：思考是模型属性，厂商参数原样写进
# 模型条目的 extra_body（见 schema.GenerationConfig._reject_thinking）。两家厂商
# 目前用同一个键名。注意这个键拼错时接口通常直接忽略，思考照开且不报错，
# 改动前先确认厂商文档。
_NO_THINKING: Dict[str, Any] = {'enable_thinking': False}

# 嵌入模型的输出维度。该模型支持 256/512/768/1024/1536/2048，不指定时返回 1024。
# 这里声明的维度必须与接口实际返回的一致，否则建出来的索引算相似度全错。
#
# 注意：嵌入请求体在 memory/embed.py 里写死为 {'input', 'model'}，不带 extra_body，
# 因此没法在配置里用 dimensions 参数把维度钉死——当前靠的是「厂商默认值恰好是
# 1024」。厂商改默认值时这里会静默失配。
_EMBEDDING_DIM = 1024

# 新装预填的模型条目：(条目名, 厂商接口的真实模型 ID, 所属厂商, 额外字段)。
# 条目名不带版本号：厂商换代时只需改 model_identifier，不必顺着改 model_tasks。
_SEED_MODELS: List[tuple[str, str, str, Dict[str, Any]]] = [
    ('deepseek-pro', 'deepseek-v4-pro-0813', _DASHSCOPE_PROVIDER, {}),
    ('deepseek-flash', 'deepseek-v4-flash-0731', _DASHSCOPE_PROVIDER, {}),
    ('qwen-flash', 'qwen3.8-flash', _DASHSCOPE_PROVIDER, {}),
    ('qwen-max', 'qwen3.8-max', _DASHSCOPE_PROVIDER, {}),
    ('qwen-vision', 'qwen3.8-max-0902', _DASHSCOPE_PROVIDER, {'visual': True}),
    ('qwen-embedding', 'qwen3.7-text-embedding', _DASHSCOPE_PROVIDER,
     {'embedding_dim': _EMBEDDING_DIM, 'extra_body': {}}),
]

# 任务到候选模型的预填分配。分档依据是「这一步值不值得为质量多付钱」：
#
# - 对话、回复生成、主动搭话直接产出她说的话，质量档最高。
# - 决策、摘要、场景观察每回合都跑且不面向用户，走快档。
# - 表达选择只做短文本判别，用最便宜的一档。
# - 记忆与日程要长上下文和稳定的结构化输出。
# - 视觉与嵌入各有专用模型，不与上面共用。
# - tts 留空：它需要 volcengine 这类专门的语音厂商，预填一个 OpenAI 兼容的
#   模型 ID 没有意义。任务候选为空时对应功能直接不启用。
_SEED_TASKS: Dict[str, List[str]] = {
    'chat': ['deepseek-pro'],
    'replyer': ['deepseek-pro'],
    'proactive': ['deepseek-pro'],
    'planner': ['deepseek-flash'],
    'summary': ['deepseek-flash'],
    'scene': ['deepseek-flash'],
    'expression': ['qwen-flash'],
    'memory': ['qwen-max'],
    'schedule': ['qwen-max'],
    'vision': ['qwen-vision'],
    'embedding': ['qwen-embedding'],
}


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


def _seed_provider(name: str, base_url: str) -> Dict[str, Any]:
    """给出一条预填厂商连接的初值。

    ``base_url`` 显式写出而不是靠 ``kind`` 的预设兜底：模板要让人一眼看见地址，
    换厂商时也知道该改哪一行。``api_key`` 保持为空——那是唯一别人替不了的东西。

    :param name: 厂商条目名，模型条目通过 ``api_provider`` 引用它。
    :param base_url: 该厂商 OpenAI 兼容端点的基础地址。
    :return: 可交给带注释 TOML 写入器的字典。
    """
    return _default_document(ApiProviderConfig, {
        'name': name,
        # kind 与条目名取同一个值：预设标识和厂商名一致，读配置的人不用两头对。
        'kind': name,
        'base_url': base_url,
    })


def _seed_model(
    name: str,
    identifier: str,
    provider: str,
    overrides: Dict[str, Any],
) -> Dict[str, Any]:
    """给出一个预填模型条目的初值。

    所有条目默认关掉思考：``extra_body`` 里写厂商参数，这是本项目唯一的思考开关
    （任务级的 ``thinking`` 字段已经取消）。``overrides`` 里若显式给了 ``extra_body``
    则以它为准，用于嵌入这类不该带生成参数的模型。

    :param name: 条目名，``model_tasks`` 通过它引用本模型。
    :param identifier: 厂商接口接受的真实模型 ID。
    :param provider: 所属厂商条目名。
    :param overrides: 该模型特有的字段，如 ``visual`` 或 ``embedding_dim``。
    :return: 可交给带注释 TOML 写入器的字典。
    """
    seed: Dict[str, Any] = {
        'name': name,
        'api_provider': provider,
        'model_identifier': identifier,
        'extra_body': dict(_NO_THINKING),
    }
    seed.update(overrides)
    return _default_document(ModelDefinitionConfig, seed)


def _bot_document() -> Dict[str, Any]:
    """组装 bot.toml 的初始文档。

    人格与称呼全部留空：它们是这个项目最个人化的部分，预填一份别人的设定，
    用户多半不会逐条清空，而是带着它跑起来。
    """
    return _default_document(BotDocument, {
        'inner': {'version': CONFIG_VERSION},
        'bot': {'name': '月璃'},
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
    """组装 features.toml 初始文档，但故意不向全新安装暴露开发者命令段。

    `developer` 在 schema 中有关闭态默认值，因此旧配置与首次安装都能正常加载；
    只有开发者手写该段并显式开启后，用户机器上才可能命中聊天内命令。
    """
    document = _default_document(FeatureDocument, {'inner': {'version': CONFIG_VERSION}})
    document.pop('developer')
    return document


def _provider_document() -> Dict[str, Any]:
    """组装 providers.toml 的初始文档，含一条待填写的连接。"""
    return _default_document(ProviderCatalog, {
        'inner': {'version': CONFIG_VERSION},
        'api_providers': [_seed_provider(_DASHSCOPE_PROVIDER, DASHSCOPE_BASE_URL)],
    })


def _model_document() -> Dict[str, Any]:
    """组装 models.toml 的初始文档。

    按 :data:`_SEED_TASKS` 给各任务分配候选。不在表里的任务候选留空——任务没有
    候选时对应功能直接不启用，比指向一个没配好的模型更容易排查。
    """
    document = _default_document(ModelCatalog, {
        'inner': {'version': CONFIG_VERSION},
        'models': [_seed_model(*entry) for entry in _SEED_MODELS],
    })
    for task, model_list in _SEED_TASKS.items():
        document['model_tasks'][task]['model_list'] = list(model_list)
    return document


def _adapter_document() -> Dict[str, Any]:
    """组装 QQ 适配器连接配置的初始文档。

    ``enabled`` 取 false：连接协议端会用真实 QQ 号登录，不能在用户确认之前发生。
    """
    return _default_document(AdapterDocument, {
        'inner': {'version': NAPCAT_CONFIG_VERSION},
        'napcat': {
            'enabled': False,
            # 占位号：显然不是真号，但把格式说清楚了（纯数字，不带任何前缀）。
            # 与 owner.qq 取同一个值是刻意的——两者相同会在 enabled 置为 true 时
            # 被互斥校验拦下并指名道姓报错，比留空后默默连上一个陌生号安全。
            'self_qq': '114514',
            'host': '127.0.0.1',
            'port': 8095,
            'token': '',
            'reconnect_interval_sec': 5.0,
            'action_timeout_sec': 15.0,
        },
        'owner': {'qq': '114514'},
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
    path.write_text(_adapter_selection_text(DEFAULT_ADAPTER_PLUGIN), encoding='utf-8')
    return DEFAULT_ADAPTER_PLUGIN, True


def _adapter_selection_text(plugin_dir: str) -> str:
    """渲染适配器声明文件的完整正文。

    这份声明只有一个字段，不套 ``[inner].version``：它不随配置结构演进，
    加一层版本壳只会多一处需要同步升级的地方。

    抽成独立函数是为了让首次启动的创建路径与 ``render_example_configs`` 渲染的
    入库模板共用同一份正文——两处各写一份必然漂移。

    :param plugin_dir: 要写入声明的插件目录名。
    :return: 含尾随换行的完整文件内容。
    """
    return '\n'.join([
        '# 当前启用的 QQ 适配器：adapters/ 下的插件目录名。',
        '# 两个协议端后端互斥，同时只能开一个；桌宠与主体都读这一处声明。',
        f'{ADAPTER_SELECTION_FIELD} = "{plugin_dir}"',
        '',
    ])


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


def render_example_configs(dest: Path) -> List[Path]:
    """把一份全新安装会生成的配置渲染到指定目录，作为随代码分发的配置模板。

    与 :func:`bootstrap_config_directory` 的差别只在落点与覆盖策略：那个函数写进
    真实配置目录、且只补缺失文件（覆盖会冲掉用户已经填好的密钥）；本函数总是重写
    目标目录下的全部文件，并把 ``adapters/`` 下每个适配器的连接配置都渲染出来——
    真实安装只生成当前启用的那一个，而模板要让人看全两种协议端各自需要填什么。

    渲染复用同一套 schema 与带注释 TOML 写入器，模板因此不可能与代码漂移；
    ``pytests/core/test_config_example.py`` 会重新渲染一次并要求与入库副本逐字一致，
    字段增删忘了重新生成模板时那条用例会红。

    :param dest: 模板输出目录；不存在时递归创建，已存在的同名文件会被覆盖。
    :return: 本次写出的文件路径列表。
    :raises KeyError: schema 新增了必填字段但本模块没有给出初值。
    :raises ValueError: 某个适配器目录的清单不是适配器清单。
    :raises OSError: 目录或文件无法写入。
    副作用：只写 dest 下的文件；不触碰真实配置目录，也不触碰 adapters/ 下的任何文件。
    """
    dest.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    selection = dest / ADAPTER_SELECTION_FILENAME
    selection.write_text(_adapter_selection_text(DEFAULT_ADAPTER_PLUGIN), encoding='utf-8')
    written.append(selection)

    documents = {
        'bot.toml': _bot_document(),
        'features.toml': _feature_document(),
        'providers.toml': _provider_document(),
        'models.toml': _model_document(),
    }
    for name in MAIN_CONFIG_FILES:
        path = dest / name
        _write_documented_toml(path, file_schema(name), documents[name], CONFIG_VERSION)
        written.append(path)

    # 适配器的连接配置与插件同目录，模板里改为按插件名平铺在 adapters/ 下：
    # 模板目录不是可运行的配置目录，照搬 adapters/<名>/config.toml 的嵌套只会
    # 让人误以为可以整个拷进 adapters/ 覆盖掉插件源码。
    adapters_dir = dest / 'adapters'
    adapters_dir.mkdir(exist_ok=True)
    for plugin_dir in sorted(
        entry.name for entry in ADAPTERS_ROOT.iterdir()
        if (entry / '_manifest.json').is_file()
    ):
        section = adapter_config_section(plugin_dir)
        path = adapters_dir / (plugin_dir + '.toml')
        _write_documented_toml(
            path,
            _adapter_write_schema(section),
            _adapter_document_with_section(_adapter_document(), section),
            NAPCAT_CONFIG_VERSION,
        )
        written.append(path)

    return written


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
    # 只差密钥的连接名。新装种子已经预填了厂商、地址与模型 ID，绝大多数情况下
    # 缺的就只有这一项；笼统地把三个字段一起报出来会让人以为还有别的要填。
    awaiting_key: List[str] = []
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
        awaiting_key.append(str(connection.get('name', '')))
    # 密钥要一次报全，不能只报对话任务用到的那一条。
    #
    # 现象：预填了两家厂商，用户照提示填完其中一个就重启，结果撞上另一家的
    #   「auth_type=bearer 时 api_key 不能为空」硬报错。
    # 原因：schema 对目录里每个厂商都要求非空密钥，不区分是否被任务引用；
    #   而这里原先只顺着 chat 候选那条链找。
    # 后果：填一次报一次，用户以为配置是坏的。
    unkeyed = [
        str(item.get('name', ''))
        for item in providers.get('api_providers', [])
        if isinstance(item, dict)
        and str(item.get('auth_type', 'bearer')) != 'none'
        and not str(item.get('api_key', '')).strip()
    ]
    if unkeyed:
        names = '」「'.join(dict.fromkeys(unkeyed))
        missing.append(
            f'providers.toml 里厂商「{names}」的 api_key：填上你自己的密钥。'
            '厂商地址与六个模型条目都已预填好，换厂商才需要一起改'
        )
    if not usable and not awaiting_key:
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
    'render_example_configs',
]
