"""加载并组合配置，启动时统一校验，字段缺失或引用错误立即报错。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import sys
import tomllib

from .schema import (
    ApiProviderConfig,
    BotDocument,
    Config,
    FeatureDocument,
    LlmConfig,
    ModelCatalog,
    ModelDefinitionConfig,
    ProviderCatalog,
    TtsConfig,
    VectorConfig,
    VisionConfig,
)

_config: Config | None = None


def _read_toml(path: Path) -> Dict[str, Any]:
    with open(path, 'rb') as file:
        return tomllib.load(file)


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


def _selected_model(
    task: str,
    model_name: str,
    models: Dict[str, ModelDefinitionConfig],
) -> ModelDefinitionConfig:
    try:
        return models[model_name]
    except KeyError as exc:
        raise ValueError(f'models.toml 的 model_tasks.{task} 引用了不存在的模型：{model_name}') from exc


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


def _load_split_config(directory: Path) -> Config:
    providers_document = ProviderCatalog.model_validate(
        _read_toml(directory / 'providers.toml')
    )
    models_document = ModelCatalog.model_validate(_read_toml(directory / 'models.toml'))
    bot_document = BotDocument.model_validate(_read_toml(directory / 'bot.toml'))
    features_document = FeatureDocument.model_validate(_read_toml(directory / 'features.toml'))

    providers = _providers_by_name(providers_document)
    models = _models_by_name(models_document)
    # 不只校验当前任务引用；未选中的坏模型同样属于配置错误，不能等切换后才暴露。
    for model in models.values():
        _selected_provider(model, providers)

    tasks = models_document.model_tasks
    chat_model = _selected_model('chat', tasks.chat, models)
    vision_model = _selected_model('vision', tasks.vision, models)
    tts_model = _selected_model('tts', tasks.tts, models)
    embedding_model = _selected_model('embedding', tasks.embedding, models)
    chat_provider = _selected_provider(chat_model, providers)
    vision_provider = _selected_provider(vision_model, providers)
    tts_provider = _selected_provider(tts_model, providers)
    embedding_provider = _selected_provider(embedding_model, providers)

    # 豆包语音是私有协议，只能承载 tts。指到别的任务上只会在运行时抛出
    # 难以定位的错误，不如在加载阶段就说清楚。
    for task, provider in (('chat', chat_provider), ('vision', vision_provider),
                            ('embedding', embedding_provider)):
        if provider.client_type != 'openai':
            raise ValueError(
                f'model_tasks.{task} 指向的厂商 {provider.name} 的 '
                f'client_type={provider.client_type}，该协议只支持 tts 任务'
            )

    features_tts = features_document.tts
    features_vision = features_document.vision
    features_vector = features_document.vector
    return Config(
        bot=bot_document.bot,
        personality=bot_document.personality,
        conversation=bot_document.conversation,
        generation=models_document.generation,
        llm=LlmConfig(
            provider=chat_provider.kind,
            model=chat_model.model_identifier,
            base_url=chat_provider.base_url,
            api_key=chat_provider.api_key,
            thinking=chat_model.thinking,
            timeout_ms=chat_provider.timeout_ms,
            max_retries=chat_provider.max_retries,
            retry_interval_ms=chat_provider.retry_interval_ms,
        ),
        tts=TtsConfig(
            enabled=features_tts.enabled,
            base_url=tts_provider.base_url,
            api_key=tts_provider.api_key,
            model=tts_model.model_identifier,
            voice=features_tts.voice,
            format=features_tts.format,
            speed=features_tts.speed,
            client_type=tts_provider.client_type,
            app_id=tts_provider.app_id,
            cluster=features_tts.cluster,
        ),
        vision=VisionConfig(
            enabled=features_vision.enabled,
            model=vision_model.model_identifier,
            api_key='' if vision_provider.name == chat_provider.name else vision_provider.api_key,
            base_url='' if vision_provider.name == chat_provider.name else vision_provider.base_url,
            timeout_ms=vision_provider.timeout_ms,
            max_retries=vision_provider.max_retries,
            retry_interval_ms=vision_provider.retry_interval_ms,
            fullscreen_silent=features_vision.fullscreen_silent,
            capture_mode=features_vision.capture_mode,
        ),
        vector=VectorConfig(
            enabled=features_vector.enabled,
            embedding_base_url=(
                '' if embedding_provider.name == chat_provider.name else embedding_provider.base_url
            ),
            embedding_api_key=(
                '' if embedding_provider.name == chat_provider.name else embedding_provider.api_key
            ),
            embedding_model=embedding_model.model_identifier,
            embedding_dim=embedding_model.embedding_dim,
        ),
        advanced=features_document.advanced,
    )


def load_config(path: Path) -> Config:
    """
    从显式路径加载并返回全局配置单例。

    目录使用新版四文件结构；普通文件继续按旧版 config.toml 读取，供迁移前
    启动探针和第三方调用方平滑过渡。同一进程内只加载一次。
    """
    global _config
    if _config is not None:
        return _config

    try:
        if path.is_dir():
            _config = _load_split_config(path)
        else:
            _config = Config.model_validate(_read_toml(path))
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
