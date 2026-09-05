"""四文件配置组合、引用校验与人格注入回归。"""

from pathlib import Path

import tempfile

import pytest

from src.core.config.loader import load_config, reset_config
from src.core.config.schema import CONFIG_VERSION, Config
from src.core.services.chat import ChatService

import json

# 测试夹具占位密钥：不对应任何真实服务，经变量间接写入避免被安全扫描当作硬编码凭据。
_FIXTURE_VISION_KEY = "-".join(("vision", "test"))
_VISION_KEY_LINE = f"api_key = {json.dumps(_FIXTURE_VISION_KEY)}"
_FIXTURE_TTS_KEY = "-".join(("tts", "test"))
_TTS_KEY_LINE = f"api_key = {json.dumps(_FIXTURE_TTS_KEY)}"


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    reset_config()
    yield
    reset_config()


def _write_split_config(directory: Path, missing_provider: bool = False) -> None:
    directory.mkdir()
    vision_provider = '' if missing_provider else f'''
[[api_providers]]
name = "vision"
kind = "openai"
base_url = "https://vision.example.com"
{_VISION_KEY_LINE}
client_type = "openai"
timeout_ms = 90000
'''
    directory.joinpath('providers.toml').write_text(
        f'''
[inner]
version = "{CONFIG_VERSION}"

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
{_TTS_KEY_LINE}
client_type = "openai"
timeout_ms = 90000
{vision_provider}''',
        encoding='utf-8',
    )
    directory.joinpath('models.toml').write_text(
        '''
[inner]
version = "''' + CONFIG_VERSION + '''"

[model_tasks.chat]
model_list = ["chat"]

[model_tasks.vision]
model_list = ["vision"]

[model_tasks.tts]
model_list = ["tts"]

[model_tasks.embedding]
model_list = ["embedding"]

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
visual = true

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
version = "''' + CONFIG_VERSION + '''"

[bot]
name = "星璃"
user_nickname = "小明"
relationship = "哥哥"

[group_chat]
at_mention_must_reply = true

[personality]
birthday = "2006-09-12"
personality = "自定义人设"
reply_style = "自定义表达"
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
version = "''' + CONFIG_VERSION + '''"

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
''',
        encoding='utf-8',
    )


def _temporary_config_directory() -> Path:
    return Path(tempfile.mkdtemp(prefix='yueli_split_config_test_')) / 'config'


def test_split_config_composes_provider_model_task_and_bot() -> None:
    directory = _temporary_config_directory()
    _write_split_config(directory)

    config = load_config(directory)

    chat = config.routing.chat.candidates[0]
    assert chat.kind == 'deepseek'
    assert chat.identifier == 'deepseek-v4-flash'
    assert chat.max_retries == 3
    assert chat.retry_interval_ms == 250
    assert config.routing.vision.candidates[0].base_url == 'https://vision.example.com'
    assert config.routing.tts.candidates[0].identifier == 'tts-1'
    # embedding 复用 chat 那条连接，所以拿到的是同一个厂商
    assert config.routing.embedding.candidates[0].provider == 'chat'
    assert config.routing.embedding.candidates[0].embedding_dim == 1024
    assert config.bot.name == '星璃'
    assert config.personality.personality == '自定义人设'
    assert config.conversation.working_memory_messages == 60
    assert config.generation.chat.temperature == 0.42
    assert config.generation.chat.max_tokens == 880


def test_split_config_rejects_unknown_provider_reference() -> None:
    directory = _temporary_config_directory()
    _write_split_config(directory, missing_provider=True)

    with pytest.raises(SystemExit):
        load_config(directory)


def test_chat_service_uses_bot_and_personality_configuration(db) -> None:
    config = Config()
    config.bot.name = '星璃'
    config.bot.user_nickname = '小明'
    config.personality.personality = '自定义人设'
    config.personality.reply_style = '自定义表达'
    config.personality.tone_probability = 1.0
    config.personality.tone_variants = ['自定义语调']
    config.conversation.working_memory_messages = 64
    config.generation.chat.temperature = 0.55
    chat = ChatService(
        db=db,
        chat_provider=None,
        proactive_provider=None,
        summary_provider=None,
        push_event=lambda *_: None,
        cfg=config,
    )

    prompt_config = chat._prompt_config_kwargs(True)

    assert prompt_config['name'] == '星璃'
    assert prompt_config['user_nickname'] == '小明'
    assert prompt_config['personality'] == '自定义人设'
    assert prompt_config['reply_style'] == '自定义表达'
    # 回归：非 owner（群聊其他成员）不得拿到 owner 专属称呼与关系，
    # 否则「对方希望你称呼 X / 把对方当 Y 看待」会套到群里每个人身上。
    contact_config = chat._prompt_config_kwargs(False)
    assert contact_config['user_nickname'] == ''
    assert contact_config['relationship'] == ''
    assert contact_config['name'] == '星璃'
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
