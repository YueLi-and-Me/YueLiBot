"""加载并组合配置，启动时统一校验，字段缺失或引用错误立即报错。"""

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
    providers: Dict[str, ApiProviderConfig] = {}
    for provider in catalog.api_providers:
        if not provider.name:
            raise ValueError('providers.toml 中 api_providers.name 不能为空')
        if provider.name in providers:
            raise ValueError(f'providers.toml 中 API 厂商名称重复：{provider.name}')
        providers[provider.name] = provider
    return providers


def _models_by_name(catalog: ModelCatalog) -> Dict[str, ModelDefinitionConfig]:
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
    """把一个任务的 model_list 解析成有序候选。顺序就是 TOML 里写的顺序。"""
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
    # 功能开着却一个候选都没有，是明确的配置错误：开了却不工作比直接报错更难查。
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
    # 备用向量模型必须和主力同维度，否则换厂商之后新旧向量根本没法比较，
    # 表现是召回突然变得毫无道理——比直接报错难查得多。
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
    """
    从配置目录加载并返回全局配置单例。同一进程内只加载一次。

    只接受四文件结构的目录。旧版单文件 config.toml 由 Electron 侧在启动前
    迁移成目录——放在这里再读一次只会得到一份把 [llm] 静默忽略掉的配置，
    表现是「她起来了但一句话也说不出」，比直接报错难查得多。
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
    """获取已加载的配置。须在 load_config(path) 之后调用。"""
    if _config is None:
        raise RuntimeError('配置未初始化，请先调用 load_config(path)')
    return _config


def reset_config() -> None:
    """清空缓存的单例——仅供测试用，让不同测试用例能加载不同的配置文件。"""
    global _config
    _config = None
