"""四文件配置组合、引用校验与人格注入回归。"""

from pathlib import Path

import tempfile

import pytest

from yueli.config.loader import load_config, reset_config
from yueli.config.schema import Config
from yueli.services.chat import ChatService


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    reset_config()
    yield
    reset_config()


def _write_split_config(directory: Path, missing_provider: bool = False) -> None:
    directory.mkdir()
    vision_provider = '' if missing_provider else '''
[[api_providers]]
name = "vision"
kind = "openai"
base_url = "https://vision.example.com"
api_key = "vision-test"
client_type = "openai"
timeout_ms = 90000
'''
    directory.joinpath('providers.toml').write_text(
        f'''
[inner]
version = "1.0.0"

[[api_providers]]
name = "chat"
kind = "deepseek"
base_url = "https://api.example.com"
api_key = "sk-test"
client_type = "openai"
timeout_ms = 90000
max_retries = 3
retry_interval_ms = 250

[[api_providers]]
name = "tts"
kind = "openai"
base_url = "https://tts.example.com"
api_key = "tts-test"
client_type = "openai"
timeout_ms = 90000
{vision_provider}''',
        encoding='utf-8',
    )
    directory.joinpath('models.toml').write_text(
        '''
[inner]
version = "1.0.0"

[model_tasks]
chat = "chat"
vision = "vision"
tts = "tts"
embedding = "embedding"

[generation.chat]
temperature = 0.42
max_tokens = 880

[[models]]
name = "chat"
model_identifier = "deepseek-v4-flash"
api_provider = "chat"

[[models]]
name = "vision"
model_identifier = "vision-pro"
api_provider = "vision"

[[models]]
name = "tts"
model_identifier = "tts-1"
api_provider = "tts"

[[models]]
name = "embedding"
model_identifier = "embedding-1"
api_provider = "chat"
embedding_dim = 1024
''',
        encoding='utf-8',
    )
    directory.joinpath('bot.toml').write_text(
        '''
[inner]
version = "1.0.0"

[bot]
name = "星璃"
user_nickname = "小明"
relationship = "哥哥"

[personality]
identity = "自定义身份"
behavior = "自定义行为"
reply_style = "自定义表达"
attention = "自定义注意力"
boundaries = "自定义边界"
tone_probability = 1.0
tone_variants = ["自定义语调"]

[conversation]
working_memory_messages = 60
summarize_trigger_messages = 72
summarize_batch_messages = 20
session_gap_minutes = 45
fact_recall_limit = 8
recalled_episode_limit = 3
recent_episode_limit = 2
episode_context_limit = 4
''',
        encoding='utf-8',
    )
    directory.joinpath('features.toml').write_text(
        '''
[inner]
version = "1.0.0"

[tts]
enabled = true
voice = "alloy"
format = "mp3"
speed = 1.0

[vision]
enabled = true
folder_enabled = false
fullscreen_silent = true

[vector]
enabled = true

[advanced]
log_level = "INFO"
https_proxy = ""
trace_content = false
trace_max_bytes = 8388608
''',
        encoding='utf-8',
    )


def _temporary_config_directory() -> Path:
    return Path(tempfile.mkdtemp(prefix='yueli_split_config_test_')) / 'config'


def test_split_config_composes_provider_model_task_and_bot() -> None:
    directory = _temporary_config_directory()
    _write_split_config(directory)

    config = load_config(directory)

    assert config.llm.provider == 'deepseek'
    assert config.llm.model == 'deepseek-v4-flash'
    assert config.vision.base_url == 'https://vision.example.com'
    assert config.tts.model == 'tts-1'
    assert config.vector.embedding_base_url == ''
    assert config.vector.embedding_dim == 1024
    assert config.bot.name == '星璃'
    assert config.personality.identity == '自定义身份'
    assert config.conversation.working_memory_messages == 60
    assert config.generation.chat.temperature == 0.42
    assert config.generation.chat.max_tokens == 880
    assert config.llm.max_retries == 3
    assert config.llm.retry_interval_ms == 250


def test_split_config_rejects_unknown_provider_reference() -> None:
    directory = _temporary_config_directory()
    _write_split_config(directory, missing_provider=True)

    with pytest.raises(SystemExit):
        load_config(directory)


def test_chat_service_uses_bot_and_personality_configuration(db) -> None:
    config = Config()
    config.bot.name = '星璃'
    config.bot.user_nickname = '小明'
    config.personality.identity = '自定义身份'
    config.personality.behavior = '自定义行为'
    config.personality.tone_probability = 1.0
    config.personality.tone_variants = ['自定义语调']
    config.conversation.working_memory_messages = 64
    config.generation.chat.temperature = 0.55
    chat = ChatService(db=db, provider=None, push_event=lambda *_: None, cfg=config)

    prompt_config = chat._prompt_config_kwargs()

    assert prompt_config['name'] == '星璃'
    assert prompt_config['user_nickname'] == '小明'
    assert prompt_config['identity'] == '自定义身份'
    assert prompt_config['behavior'] == '自定义行为'
    assert chat._working_memory_messages == 64
    assert chat._chat_temperature == 0.55


def test_conversation_rejects_summary_backlog_larger_than_working_window() -> None:
    with pytest.raises(ValueError, match='不能大于 working_memory_messages'):
        Config.model_validate({
            'conversation': {
                'working_memory_messages': 20,
                'summarize_trigger_messages': 48,
                'summarize_batch_messages': 16,
            },
        })
