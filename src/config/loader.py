"""加载四份版本化 TOML，并组合为运行时配置视图。

模块先校验所有厂商、模型和任务引用，再解析任务候选与功能开关；任何字段缺失、
模型引用错误、协议不匹配或启用功能没有候选模型都会在启动阶段报告。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import sys

from .schema import (
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
)
from .toml_io import read_versioned_toml

from src.common.logger import get_logger
from src.llm_models.openai import resolve_base_url

_config: Config | None = None
logger = get_logger(__name__)


CONFIG_VERSION = '1.1.0'
_VERSION_HINT = '正常情况下 Electron 启动时会自动升级，手工改过的话请对照模板补齐'
CHAT_INHERITING_TASKS = ('proactive', 'summary', 'schedule', 'expression')


def _providers_by_name(catalog: ProviderCatalog) -> Dict[str, ApiProviderConfig]:
    """按名称建立 API 厂商配置索引并检查名称唯一性。

    :param catalog: 已完成 Pydantic 校验的厂商目录。
    :return: 厂商名称到配置对象的字典。
    :raises ValueError: 厂商名称为空或重复。
    :side_effects: 不修改目录及其中的配置对象。
    """
    providers: Dict[str, ApiProviderConfig] = {}
    for provider in catalog.api_providers:
        if not provider.name:
            raise ValueError('providers.toml 中 api_providers.name 不能为空')
        if provider.name in providers:
            raise ValueError(f'providers.toml 中 API 厂商名称重复：{provider.name}')
        providers[provider.name] = provider
    return providers


def _models_by_name(catalog: ModelCatalog) -> Dict[str, ModelDefinitionConfig]:
    """按名称建立模型定义索引并检查名称唯一性。

    :param catalog: 已完成 Pydantic 校验的模型目录。
    :return: 模型名称到模型定义的字典。
    :raises ValueError: 模型名称为空或重复。
    :side_effects: 不修改模型目录。
    """
    models: Dict[str, ModelDefinitionConfig] = {}
    for model in catalog.models:
        if not model.name:
            raise ValueError('models.toml 中 models.name 不能为空')
        if model.name in models:
            raise ValueError(f'models.toml 中模型名称重复：{model.name}')
        models[model.name] = model
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
    :side_effects: 不修改输入映射。
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
    :side_effects: 记录继承关系日志，不写入配置文件。
    """
    routing = getattr(models_document.model_tasks, task)
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
        provider = _selected_provider(model, providers)
        # 豆包语音是私有协议，只能承载 tts。指到别的任务上只会在运行时抛出
        # 难以定位的错误，不如在加载阶段就说清楚。
        if task != 'tts' and provider.client_type != 'openai':
            raise ValueError(
                f'model_tasks.{task} 的候选 {model_name} 指向厂商 {provider.name}，其 '
                f'client_type={provider.client_type}，该协议只支持 tts 任务'
            )
        # 豆包语音不吃 model_identifier（音色由 voice 决定），其余任务必须有模型 ID。
        if provider.client_type == 'openai' and not model.model_identifier.strip():
            raise ValueError(
                f'models.toml 中模型 {model_name} 没有填 model_identifier，'
                f'model_tasks.{task} 无法使用它'
            )
        # 地址在加载期就解析一次。留到第一次请求才发现「这个 kind 没有内置地址」，
        # 表现是轮询把每条候选都撞一遍然后整轮失败，根因藏在最后一条报错里。
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
    :side_effects: 读取配置文件并记录必要的任务继承日志，不修改磁盘内容。
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
    # 不只校验当前任务引用；未选中的坏模型同样属于配置错误，不能等轮询切到
    # 它头上、用户正等着回话的时候才暴露。
    for model in models.values():
        _selected_provider(model, providers)

    chat_routing = _build_routing('chat', models_document, models, providers, None)
    routing = RoutingConfig(
        chat=chat_routing,
        proactive=_build_routing('proactive', models_document, models, providers, chat_routing),
        summary=_build_routing('summary', models_document, models, providers, chat_routing),
        schedule=_build_routing('schedule', models_document, models, providers, chat_routing),
        expression=_build_routing('expression', models_document, models, providers, chat_routing),
        vision=_build_routing('vision', models_document, models, providers, None),
        tts=_build_routing('tts', models_document, models, providers, None),
        embedding=_build_routing('embedding', models_document, models, providers, None),
    )

    features_tts = features_document.tts
    features_vision = features_document.vision
    features_perception = features_document.perception
    features_vector = features_document.vector
    # 已启用功能必须至少绑定一个候选模型；否则配置表面有效，但运行时无法执行该功能。
    for enabled, task in ((features_tts.enabled, 'tts'),
                          (features_vision.enabled, 'vision'),
                          (features_vector.enabled, 'embedding')):
        if enabled and not getattr(routing, task).ready:
            raise ValueError(
                f'features.toml 里启用了该功能，但 models.toml 的 '
                f'model_tasks.{task}.model_list 是空的'
            )
    if features_tts.enabled and not features_tts.voice.strip():
        raise ValueError('features.toml 里启用了 tts，但没有填 voice（音色 ID）')
    # 备用向量模型必须与主模型保持相同维度，否则不同模型生成的向量无法进行有效比较，
    # 召回排序将失去可比性；在加载期拒绝该配置可以避免运行时产生隐蔽错误。
    dims = {candidate.embedding_dim for candidate in routing.embedding.candidates}
    if len(dims) > 1:
        raise ValueError(
            f'model_tasks.embedding 的候选模型 embedding_dim 不一致：{sorted(dims)}；'
            '备用向量模型必须和主力输出同样的维度'
        )

    return Config(
        bot=bot_document.bot,
        group_chat=bot_document.group_chat,
        schedule=bot_document.schedule,
        personality=bot_document.personality,
        conversation=bot_document.conversation,
        generation=models_document.generation,
        routing=routing,
        tts=features_tts,
        vision=features_vision,
        perception=features_perception,
        vector=features_vector,
        log=features_document.log,
        advanced=features_document.advanced,
    )


def load_config(path: Path) -> Config:
    """从配置目录加载并缓存全局配置单例。

    仅接受由四个 TOML 文件组成的配置目录。旧版单文件配置必须在进入本模块前完成
    迁移；直接按新结构读取会丢失模型引用，因此目录结构不符合要求时显式失败。

    Args:
        path: 配置目录路径。

    Returns:
        进程级缓存的 ``Config``；同一进程后续调用返回同一对象。

    Raises:
        SystemExit: 路径不是目录、配置读取或字段校验失败时以状态码 ``1`` 退出。

    Side Effects:
        首次调用读取配置文件并写入模块级缓存；失败时向标准错误输出诊断信息。
    """
    global _config
    if _config is not None:
        return _config

    try:
        if not path.is_dir():
            raise ValueError(
                f'{path} 不是配置目录；需要包含 providers/models/bot/features 四份 TOML'
            )
        _config = _load_split_config(path)
    except Exception as exc:
        print(f'[yueli] 配置错误，请检查 {path}：\n{exc}', file=sys.stderr)
        sys.exit(1)

    return _config


def get_config() -> Config:
    """返回当前进程缓存的已加载配置。

    :return: 最近一次由 :func:`load_config` 成功加载的配置对象。
    :raises RuntimeError: 尚未调用 :func:`load_config` 或加载尚未成功。
    :side_effects: 不读取磁盘，不修改配置。
    """
    if _config is None:
        raise RuntimeError('配置未初始化，请先调用 load_config(path)')
    return _config


def reset_config() -> None:
    """清空进程级配置缓存。

    :return: 无返回值。
    :side_effects: 将后续 :func:`get_config` 置为未初始化；仅供测试隔离配置使用。
    """
    global _config
    _config = None
