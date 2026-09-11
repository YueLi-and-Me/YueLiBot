"""加载四份版本化 TOML，并组合为运行时配置视图。

模块先校验所有厂商、模型和任务引用，再解析任务候选与功能开关；任何字段缺失、
模型引用错误、协议不匹配或启用功能没有候选模型都会在启动阶段报告。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Tuple

import sys

from pydantic import BaseModel

from .schema import (
    CONFIG_VERSION,
    ApiProviderConfig,
    BotDocument,
    Config,
    FeatureDocument,
    ModelCandidate,
    ModelCatalog,
    ModelDefinitionConfig,
    ProviderCatalog,
    RoutingConfig,
    TaskRouting,
    TaskRoutingConfig,
)
from .toml_io import read_versioned_toml

from src.core.logging.logger import get_logger
from src.core.llm_models.openai import resolve_base_url

_config: Config | None = None
_config_dir: Path | None = None
logger = get_logger(__name__)


# 版本号定义在 schema 里，这里只导入：两处各写一份必然漂移。
_VERSION_HINT = '正常情况下 Electron 启动时会自动升级，手工改过的话请对照模板补齐'
CHAT_INHERITING_TASKS = (
    'proactive', 'summary', 'schedule', 'expression', 'planner', 'replyer', 'scene',
    'memory',
)


def _model_task_config(
    catalog: ModelCatalog,
    task: str,
) -> TaskRoutingConfig:
    """按封闭任务名取得模型目录中的任务配置。"""
    tasks = {
        'chat': catalog.model_tasks.chat,
        'proactive': catalog.model_tasks.proactive,
        'summary': catalog.model_tasks.summary,
        'schedule': catalog.model_tasks.schedule,
        'vision': catalog.model_tasks.vision,
        'expression': catalog.model_tasks.expression,
        'planner': catalog.model_tasks.planner,
        'replyer': catalog.model_tasks.replyer,
        'scene': catalog.model_tasks.scene,
        'memory': catalog.model_tasks.memory,
        'tts': catalog.model_tasks.tts,
        'embedding': catalog.model_tasks.embedding,
    }
    try:
        return tasks[task]
    except KeyError as exc:
        raise ValueError(f'未知模型任务：{task}') from exc


def _providers_by_name(catalog: ProviderCatalog) -> Dict[str, ApiProviderConfig]:
    """按名称建立 API 厂商配置索引并检查名称唯一性。

    :param catalog: 已完成 Pydantic 校验的厂商目录。
    :return: 厂商名称到配置对象的字典。
    :raises ValueError: 厂商名称为空或重复，并指出在 providers.toml 中的条目位置。
    副作用：不修改目录及其中的配置对象。
    """
    providers: Dict[str, ApiProviderConfig] = {}
    positions: Dict[str, int] = {}
    for index, provider in enumerate(catalog.api_providers, start=1):
        if not provider.name:
            raise ValueError(f'providers.toml 中第 {index} 个 [api_providers] 的 name 不能为空')
        if provider.name in providers:
            raise ValueError(
                f'providers.toml 中 API 厂商名称重复：{provider.name}'
                f'（第 {positions[provider.name]} 个与第 {index} 个 [api_providers]）'
            )
        providers[provider.name] = provider
        positions[provider.name] = index
    return providers


def _models_by_name(catalog: ModelCatalog) -> Dict[str, ModelDefinitionConfig]:
    """按名称建立模型定义索引并检查名称唯一性。

    :param catalog: 已完成 Pydantic 校验的模型目录。
    :return: 模型名称到模型定义的字典。
    :raises ValueError: 模型名称为空或重复，并指出在 models.toml 中的条目位置。
    副作用：不修改模型目录。
    """
    models: Dict[str, ModelDefinitionConfig] = {}
    positions: Dict[str, int] = {}
    for index, model in enumerate(catalog.models, start=1):
        if not model.name:
            raise ValueError(f'models.toml 中第 {index} 个 [models] 的 name 不能为空')
        if model.name in models:
            raise ValueError(
                f'models.toml 中模型名称重复：{model.name}'
                f'（第 {positions[model.name]} 个与第 {index} 个 [models]）'
            )
        models[model.name] = model
        positions[model.name] = index
    return models


def _selected_provider(
    model: ModelDefinitionConfig,
    providers: Dict[str, ApiProviderConfig],
) -> ApiProviderConfig:
    """解析模型引用的 API 厂商。

    :param model: 需要解析厂商引用的模型定义。
    :param providers: 厂商名称索引。
    :return: `model.api_provider` 对应的厂商配置。
    :raises ValueError: 模型引用了不存在的厂商。
    副作用：不修改输入映射。
    """
    try:
        return providers[model.api_provider]
    except KeyError as exc:
        raise ValueError(
            f'models.toml 中模型 {model.name} 引用了不存在的 API 厂商：{model.api_provider}'
        ) from exc


def _build_routing(
    task: str,
    models_document: ModelCatalog,
    models: Dict[str, ModelDefinitionConfig],
    providers: Dict[str, ApiProviderConfig],
    chat_routing: TaskRouting | None,
) -> TaskRouting:
    """把一个任务的模型名列表解析为有序运行时候选。

    :param task: 任务名称，例如 `chat`、`vision` 或 `tts`。
    :param models_document: 已校验的模型目录及任务路由配置。
    :param models: 模型名称索引。
    :param providers: API 厂商名称索引。
    :param chat_routing: 可供继承的 chat 路由；非 chat 任务为空列表时必须提供。
    :return: 按 TOML 列表顺序排列的运行时任务路由。
    :raises ValueError: 模型/厂商引用缺失、协议类型不匹配、模型 ID 缺失、继承路由缺失
        或向量候选维度等配置不合法。
    副作用：记录继承关系日志，不写入配置文件。
    """
    routing = _model_task_config(models_document, task)
    if task in CHAT_INHERITING_TASKS and not routing.model_list:
        if chat_routing is None:
            raise ValueError(f'model_tasks.{task} 缺少可继承的 chat 路由')
        logger.info('model_task_inherits_chat', task=task)
        return TaskRouting(
            task=task,
            candidates=chat_routing.candidates,
            strategy=chat_routing.strategy,
            first_token_timeout_ms=routing.first_token_timeout_ms,
            slow_threshold_ms=routing.slow_threshold_ms,
        )

    candidates = []
    for model_name in routing.model_list:
        try:
            model = models[model_name]
        except KeyError as exc:
            raise ValueError(
                f'models.toml 的 model_tasks.{task}.model_list 引用了不存在的模型：{model_name}'
            ) from exc
        # visual 是模型目录对图片输入能力的显式声明。只在 WebUI 里过滤还不够：
        # 手工编辑 TOML 仍可能把纯文本模型放进视觉路由，最终让模型把图片当作
        # Unsupported Image。加载期直接拒绝，避免错误描述进入聊天上下文和缓存。
        if task == 'vision' and not model.visual:
            raise ValueError(
                f'model_tasks.vision 的候选 {model_name} 没有标记 visual = true，'
                '不能用于屏幕视觉或聊天图片理解'
            )
        provider = _selected_provider(model, providers)
        # 豆包语音是私有协议，只能承载 tts。指到别的任务上只会在运行时抛出
        # 难以定位的错误，不如在加载阶段就说清楚。
        if task != 'tts' and provider.client_type != 'openai':
            raise ValueError(
                f'model_tasks.{task} 的候选 {model_name} 指向厂商 {provider.name}，其 '
                f'client_type={provider.client_type}，该协议只支持 tts 任务'
            )
        # 豆包语音不使用 model_identifier（音色由 voice 决定），其余任务必须有模型 ID。
        if provider.client_type == 'openai' and not model.model_identifier.strip():
            raise ValueError(
                f'models.toml 中模型 {model_name} 没有填 model_identifier，'
                f'model_tasks.{task} 无法使用它'
            )
        # 地址在加载期就解析一次。留到第一次请求才发现「这个 kind 没有内置地址」，
        # 表现为轮询逐条尝试全部候选后整轮失败，根因藏在最后一条报错里。
        if provider.client_type == 'openai':
            resolve_base_url(provider.kind, provider.base_url)
        candidates.append(ModelCandidate(
            name=model.name,
            provider=provider.name,
            kind=provider.kind,
            base_url=provider.base_url,
            api_key=provider.api_key,
            auth_type=provider.auth_type,
            auth_name=provider.auth_name,
            identifier=model.model_identifier.strip(),
            extra_body=model.extra_body,
            reasoning_parse_mode=model.reasoning_parse_mode,
            visual=model.visual,
            temperature=model.temperature,
            max_tokens=model.max_tokens,
            default_headers=provider.default_headers,
            default_query=provider.default_query,
            client_type=provider.client_type,
            app_id=provider.app_id,
            embedding_dim=model.embedding_dim,
            timeout_ms=provider.timeout_ms,
            max_retries=provider.max_retries,
            retry_interval_ms=provider.retry_interval_ms,
        ))
    return TaskRouting(
        task=task,
        candidates=candidates,
        strategy=routing.selection_strategy,
        first_token_timeout_ms=routing.first_token_timeout_ms,
        slow_threshold_ms=routing.slow_threshold_ms,
    )


def _load_split_config(directory: Path) -> Config:
    """读取四份 TOML 并组合为完整运行时配置。

    :param directory: 同时包含 `providers.toml`、`models.toml`、`bot.toml` 和
        `features.toml` 的配置目录。
    :return: 已校验的 :class:`Config`。
    :raises ValueError: 版本、字段、引用、任务路由或功能开关配置不一致。
    :raises OSError: 配置文件无法读取。
    副作用：读取配置文件并记录必要的任务继承日志，不修改磁盘内容。
    """
    providers_document = ProviderCatalog.model_validate(
        read_versioned_toml(directory / 'providers.toml', CONFIG_VERSION, _VERSION_HINT)
    )
    models_document = ModelCatalog.model_validate(
        read_versioned_toml(directory / 'models.toml', CONFIG_VERSION, _VERSION_HINT)
    )
    bot_document = BotDocument.model_validate(
        read_versioned_toml(directory / 'bot.toml', CONFIG_VERSION, _VERSION_HINT)
    )
    features_document = FeatureDocument.model_validate(
        read_versioned_toml(directory / 'features.toml', CONFIG_VERSION, _VERSION_HINT)
    )

    providers = _providers_by_name(providers_document)
    models = _models_by_name(models_document)
    # 不只校验当前任务引用；未选中的坏模型同样属于配置错误，不能等到轮询切换到
    # 该模型、用户正在等待回复时才暴露。
    for model in models.values():
        _selected_provider(model, providers)

    chat_routing = _build_routing('chat', models_document, models, providers, None)
    routing = RoutingConfig(
        chat=chat_routing,
        proactive=_build_routing('proactive', models_document, models, providers, chat_routing),
        summary=_build_routing('summary', models_document, models, providers, chat_routing),
        schedule=_build_routing('schedule', models_document, models, providers, chat_routing),
        expression=_build_routing('expression', models_document, models, providers, chat_routing),
        planner=_build_routing('planner', models_document, models, providers, chat_routing),
        replyer=_build_routing('replyer', models_document, models, providers, chat_routing),
        scene=_build_routing('scene', models_document, models, providers, chat_routing),
        memory=_build_routing('memory', models_document, models, providers, chat_routing),
        vision=_build_routing('vision', models_document, models, providers, None),
        tts=_build_routing('tts', models_document, models, providers, None),
        embedding=_build_routing('embedding', models_document, models, providers, None),
    )

    features_tts = features_document.tts
    features_vision = features_document.vision
    features_perception = features_document.perception
    features_vector = features_document.vector
    feature_routes = {
        'tts': routing.tts,
        'vision': routing.vision,
        'embedding': routing.embedding,
    }
    # 已启用功能必须至少绑定一个候选模型；否则配置表面有效，但运行时无法执行该功能。
    for enabled, task in (
        (features_tts.enabled, 'tts'),
        (features_vision.enabled, 'vision'),
        (features_vision.chat_image_enabled, 'vision'),
        (features_vector.enabled, 'embedding'),
    ):
        if enabled and not feature_routes[task].ready:
            raise ValueError(
                f'features.toml 里启用了该功能，但 models.toml 的 '
                f'model_tasks.{task}.model_list 是空的'
            )
    if features_tts.enabled and not features_tts.voice.strip():
        raise ValueError('features.toml 里启用了 tts，但没有填 voice（音色 ID）')
    # 全部候选向量模型必须维度相同，否则不同模型生成的向量无法进行有效比较，
    # 召回排序将失去可比性；在加载期拒绝该配置可以避免运行时产生隐蔽错误。
    dims = {candidate.embedding_dim for candidate in routing.embedding.candidates}
    if len(dims) > 1:
        raise ValueError(
            f'model_tasks.embedding 的候选模型 embedding_dim 不一致：{sorted(dims)}；'
            '同一任务下的候选向量模型必须输出同样的维度'
        )

    return Config(
        bot=bot_document.bot,
        group_chat=bot_document.group_chat,
        schedule=bot_document.schedule,
        personality=bot_document.personality,
        conversation=bot_document.conversation,
        conversation_agent=bot_document.conversation_agent,
        typing=bot_document.typing,
        emoji=bot_document.emoji,
        desktop_pet=bot_document.desktop_pet,
        generation=models_document.generation,
        routing=routing,
        tts=features_tts,
        vision=features_vision,
        perception=features_perception,
        vector=features_vector,
        memory_feedback=features_document.memory_feedback,
        telemetry=features_document.telemetry,
        log=features_document.log,
        advanced=features_document.advanced,
        developer=features_document.developer,
        update_announce=features_document.update_announce,
    )


def read_config(path: Path) -> Config:
    """只读并校验配置目录，不修改进程级配置缓存。

    运行时自检与启动加载必须共用这一入口，确保版本、字段、模型引用和功能开关
    的判断不会分成两套。

    :param path: 配置目录路径。
    :return: 完成全部交叉校验的运行时配置对象。
    :raises ValueError: 路径不是目录，或配置字段、引用、功能开关不一致。
    :raises OSError: 配置文件无法读取。
    副作用：只读配置文件并记录必要的任务继承日志，不修改文件或全局缓存。
    """
    if not path.is_dir():
        raise ValueError(
            f'{path} 不是配置目录；需要包含 providers/models/bot/features 四份 TOML'
        )
    return _load_split_config(path)


def load_config(path: Path) -> Config:
    """从配置目录加载并缓存全局配置单例。

    仅接受由四个 TOML 文件组成的配置目录。旧版单文件配置必须在进入本模块前完成
    迁移；直接按新结构读取会丢失模型引用，因此目录结构不符合要求时显式失败。

    :param path: 配置目录路径。

    :return: 进程级缓存的 ``Config``；同一进程后续调用返回同一对象。

    :raises SystemExit: 路径不是目录、配置读取或字段校验失败时以状态码 ``1`` 退出。

    副作用：
        首次调用读取配置文件并写入模块级缓存与目录记录（供热重载复用）；
        失败时向标准错误输出诊断信息。
    """
    global _config, _config_dir
    if _config is not None:
        return _config

    try:
        _config = read_config(path)
    except Exception as exc:
        print(f'[yueli] 配置错误，请检查 {path}：\n{exc}', file=sys.stderr)
        sys.exit(1)

    _config_dir = path
    return _config


def get_config() -> Config:
    """返回当前进程缓存的已加载配置。

    :return: 最近一次由 :func:`load_config` 成功加载的配置对象。
    :raises RuntimeError: 尚未调用 :func:`load_config` 或加载尚未成功。
    副作用：不读取磁盘，不修改配置。
    """
    if _config is None:
        raise RuntimeError('配置未初始化，请先调用 load_config(path)')
    return _config


def reset_config() -> None:
    """清空进程级配置缓存。

    :return: 无返回值。
    副作用：将后续 :func:`get_config` 置为未初始化并遗忘配置目录；仅供测试隔离配置使用。
    """
    global _config, _config_dir
    _config = None
    _config_dir = None


# ---------------------------------------------------------------- 配置热重载
# 三类字段（见任务包四交付报告的逐字段结论）：
# 1. 能热重载——只在使用点读取、没有拷贝残留，重载换掉持有方引用即生效；
# 2. 必须重启——进程启动时就决定形态（模型客户端、日志管道、可选服务装配）；
# 3. 需要改代码才能热重载——被拷进实例属性（chat.py:408 一类），本包不动。
# 下面前缀表只登记第 2、3 类；不在表里的即第 1 类。前缀匹配按点分段做，
# 避免 'log' 误命中 'logging' 这类同前缀字段名。
RESTART_REQUIRED_PREFIXES: Tuple[str, ...] = (
    'routing',                    # 厂商/模型/任务路由：模型客户端与熔断状态一次装配
    'tts.enabled',                # TtsService 只在启动时创建；voice/speed/format 动态读
    'vision.enabled',             # 视觉链路启动装配（关→开必须重启；开→关动态读）
    'vision.fullscreen_silent',   # 视觉采集链路启动形态
    'vision.capture_mode',        # 同上
    'vector.enabled',             # VectorService 启动装配
    'memory_feedback',            # MemoryFeedbackService 启动装配；关闭时根本不建服务
    'log',                        # 日志管道、快照与事件保留策略启动定型
)
DEFERRED_PREFIXES: Tuple[str, ...] = (
    'conversation',               # chat.py __init__ 全段拷贝
    'conversation_agent',         # 同上
    'perception',                 # surfaces 拷成 frozenset
    'generation.chat',            # 采样参数拷进实例属性
    'generation.planner',
    'generation.replyer',
    'generation.proactive',
    'bot.name',                   # 名字与别名另有拷贝路径（提及检测、摘要人格）
    'bot.aliases',
    'group_chat.scene_refresh_messages',
    'group_chat.self_started_topics',
    'group_chat.at_mention_must_reply',
    'group_chat.name_mention_probability',
    'group_chat.persona_weight',
)

# 订阅回调列表：第 1 类字段的持有方在重载成功后拿到新配置。这个项目用不上
# 观察者框架，一个模块级列表加一个注册函数就是全部机制。
_reload_listeners: List[Callable[[Config, Config], None]] = []


def add_config_reload_listener(
    listener: Callable[[Config, Config], None],
) -> Callable[[], None]:
    """登记一个配置热重载回调。

    :param listener: 回调，参数为 ``(旧配置, 新配置)``；在重载成功、全局配置
        已替换之后同步调用。
    :return: 注销函数；重复注销安全。
    副作用：追加进模块级回调列表。
    """
    _reload_listeners.append(listener)

    def _remove() -> None:
        """从回调列表移除已登记的监听器。"""
        try:
            _reload_listeners.remove(listener)
        except ValueError:
            pass

    return _remove


def _changed_leaves(path: str, old_value: object, new_value: object) -> List[str]:
    """递归比较一个字段的旧新取值，返回发生变化的叶子路径。

    pydantic 模型继续下钻到标量；列表与字典整体视为一个字段——厂商表这类
    列表重排后的逐下标路径只有噪声没有信息量，而配置里含密钥，路径一律
    不携带值。

    :param path: 当前字段的点路径。
    :param old_value: 旧配置中的取值。
    :param new_value: 新配置中的取值。
    :return: 变化字段的点路径列表；两值相等时为空。
    """
    if isinstance(old_value, BaseModel) and isinstance(new_value, BaseModel):
        paths: List[str] = []
        for name in type(old_value).model_fields:
            child_old = getattr(old_value, name)
            child_new = getattr(new_value, name)
            if child_old != child_new:
                paths.extend(_changed_leaves(f'{path}.{name}', child_old, child_new))
        return paths
    return [path]


def _annotate_change(path: str) -> str:
    """按三类前缀表给变更路径加生效性标注。

    :param path: 点路径字段名。
    :return: 带中文标注的行；第 1 类返回原路径。
    """
    for prefix in RESTART_REQUIRED_PREFIXES:
        if path == prefix or path.startswith(prefix + '.'):
            return f'{path}（需要重启才生效）'
    for prefix in DEFERRED_PREFIXES:
        if path == prefix or path.startswith(prefix + '.'):
            return f'{path}（已被运行时持有，本次重载不生效）'
    return path


def reload_config() -> Tuple[Config, List[str]]:
    """重读配置目录并整体替换全局配置，返回新配置与标注后的变更清单。

    与启动共用 :func:`_load_split_config`，因此校验完全一致：版本、引用、任务
    路由与功能开关任何一项不过都在替换之前抛出，全局配置保持原状——
    不落盘、不半应用、不做「失败就用旧配置继续跑」的静默兜底。

    :return: ``(新配置, 标注后的变更字段行列表)``；没有变化时清单为空。

    :raises Exception: 配置读取或校验失败时原样抛出，进程继续用旧配置运行。
    :raises RuntimeError: 尚未成功调用过 :func:`load_config`。

    副作用：
        校验通过后替换模块级配置单例并逐一调用订阅回调（单个回调失败只记
        ``config_reload_listener_failed`` 日志，不阻断其余回调、不回滚配置——
        配置本体已完整应用，个别持有方失联是要暴露的缺陷，不是要隐藏的状态）。
    """
    global _config
    if _config is None or _config_dir is None:
        raise RuntimeError('配置未初始化，请先调用 load_config(path)')
    previous = _config
    # 先完整构建再替换：这里抛出的任何异常都发生在全局配置被碰之前。
    fresh = read_config(_config_dir)
    changed = [
        line
        for name in type(fresh).model_fields
        for line in _changed_leaves(name, getattr(previous, name), getattr(fresh, name))
    ]
    summary = [_annotate_change(path) for path in changed]
    _config = fresh
    logger.info('config_reloaded', changedFields=summary if summary else ['（无字段变化）'])
    for listener in list(_reload_listeners):
        try:
            listener(previous, fresh)
        except Exception:
            logger.exception('config_reload_listener_failed')
    return fresh, summary
