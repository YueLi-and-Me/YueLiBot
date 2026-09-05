"""
TOML 配置加载 + bot.relationship/user_nickname 确实传进 prompt 的回归。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.core.agent.prompt import build_system_prompt
from src.core.config.loader import load_config, reset_config
from src.core.config.schema import CONFIG_VERSION, Config
from src.core.services.chat import ChatService


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    reset_config()
    yield
    reset_config()


def test_conversation_agent_enables_tool_calling_by_default() -> None:
    """A、B 默认同时启用，未写配置时直接使用工具调用决策。"""
    config = Config()

    assert config.conversation_agent.split_replyer is True
    assert config.conversation_agent.tool_calling is True


def _write_config_dir(
    bot: str = "",
    features: str = "",
    *,
    bot_name: str = '月璃',
    at_mention_must_reply: bool = True,
) -> Path:
    """写一份最小可加载的配置目录，只填必要项，其余走默认值。"""
    directory = Path(tempfile.mkdtemp(prefix="yueli_config_test_")) / "config"
    directory.mkdir()
    directory.joinpath("providers.toml").write_text(f"""
[inner]
version = "{CONFIG_VERSION}"

[[api_providers]]
name = "主力"
kind = "deepseek"
base_url = "https://api.example.com"
api_key = "sk-test"
client_type = "openai"
timeout_ms = 90000
""", encoding="utf-8")
    directory.joinpath("models.toml").write_text(f"""
[inner]
version = "{CONFIG_VERSION}"

[model_tasks.chat]
model_list = ["chat"]

[model_tasks.vision]
model_list = []

[model_tasks.tts]
model_list = ["tts"]

[model_tasks.embedding]
model_list = []

[[models]]
name = "chat"
model_identifier = "deepseek-chat"
api_provider = "主力"

[[models]]
name = "tts"
model_identifier = "tts-1"
api_provider = "主力"
""", encoding="utf-8")
    directory.joinpath("bot.toml").write_text(f"""
[inner]
version = "{CONFIG_VERSION}"

[bot]
name = "{bot_name}"
{bot}

[group_chat]
at_mention_must_reply = {str(at_mention_must_reply).lower()}

[personality]
birthday = "2006-09-12"
personality = "人设"
reply_style = "表达"
tone_probability = 0.25
tone_variants = ["语调"]
""", encoding="utf-8")
    directory.joinpath("features.toml").write_text(features or f"""
[inner]
version = "{CONFIG_VERSION}"

[tts]
enabled = false
voice = ""
format = "mp3"
speed = 0.95

[vision]
enabled = false
fullscreen_silent = true

[vector]
enabled = false

[advanced]
log_level = "INFO"
https_proxy = ""
""", encoding="utf-8")
    return directory


def test_bot_names_allow_single_characters_ascii_and_symbols() -> None:
    config = load_config(_write_config_dir(
        bot='aliases = ["星", "NOVA!"]',
        bot_name='Bot#7',
    ))

    assert config.bot.name == 'Bot#7'
    assert config.bot.aliases == ['星', 'NOVA!']


@pytest.mark.parametrize('enabled', [True, False])
def test_at_must_reply_switch_is_loaded_from_bot_toml(enabled: bool) -> None:
    config = load_config(_write_config_dir(at_mention_must_reply=enabled))

    assert config.group_chat.at_mention_must_reply is enabled


def test_at_must_reply_switch_is_required_in_bot_toml() -> None:
    directory = _write_config_dir()
    path = directory / 'bot.toml'
    content = path.read_text(encoding='utf-8')
    path.write_text(
        content.replace('at_mention_must_reply = true\n', ''),
        encoding='utf-8',
    )

    with pytest.raises(SystemExit):
        load_config(directory)


def test_load_config_parses_nested_sections():
    directory = _write_config_dir(
        bot='user_nickname = "小明"\nrelationship = "哥哥"',
        features=f"""
[inner]
version = "{CONFIG_VERSION}"

[tts]
enabled = true
voice = "alloy"
format = "mp3"
speed = 0.95

[vision]
enabled = false
fullscreen_silent = true

[vector]
enabled = false

[advanced]
log_level = "INFO"
https_proxy = ""
""",
    )
    cfg = load_config(directory)
    assert cfg.bot.user_nickname == "小明"
    assert cfg.bot.relationship == "哥哥"
    # 连接信息现在只存在于候选里，不再复制一份到任务配置上
    chat = cfg.routing.chat.candidates[0]
    assert chat.kind == "deepseek"
    assert chat.api_key == "sk-test"
    assert chat.identifier == "deepseek-chat"
    assert cfg.tts.enabled is True
    assert cfg.routing.tts.ready is True


def test_load_config_defaults_when_sections_missing():
    cfg = load_config(_write_config_dir())
    assert cfg.tts.enabled is False
    assert cfg.vision.ready is False
    assert cfg.vector.enabled is False
    assert cfg.conversation == Config().conversation


def test_load_config_exits_on_type_error():
    directory = _write_config_dir(features=f"""
[inner]
version = "{CONFIG_VERSION}"

[tts]
enabled = "not-a-bool"
voice = ""
format = "mp3"
speed = 0.95

[vision]
enabled = false
fullscreen_silent = true

[vector]
enabled = false

[advanced]
log_level = "INFO"
https_proxy = ""
""")
    with pytest.raises(SystemExit):
        load_config(directory)


def test_load_config_rejects_single_file():
    """旧版单文件由 Electron 迁移；Python 再读一次只会静默忽略 [llm]。"""
    tmp = Path(tempfile.mkdtemp(prefix="yueli_legacy_test_"))
    path = tmp / "config.toml"
    path.write_text('[llm]\nmodel = "x"\n', encoding="utf-8")

    with pytest.raises(SystemExit):
        load_config(path)


def test_prompt_includes_relationship_and_nickname_when_set():
    prompt = build_system_prompt(
        name='测试角色',
        birthday='',
        personality='',
        reply_style='',
        user_nickname="小明",
        relationship="哥哥",
    )
    assert "小明" in prompt
    assert "哥哥" in prompt


def test_prompt_omits_relationship_block_when_unset():
    prompt = build_system_prompt(
        name='测试角色',
        birthday='',
        personality='',
        reply_style='',
    )
    # 没传 user_nickname/relationship 时不应该出现"喊他"这类关系措辞
    assert "喊他" not in prompt


def test_chat_service_threads_relationship_into_prompt(db):
    cfg = Config()
    cfg.bot.user_nickname = "小明"
    cfg.bot.relationship = "哥哥"
    chat = ChatService(
        db=db,
        chat_provider=None,
        proactive_provider=None,
        summary_provider=None,
        push_event=lambda *_: None,
        cfg=cfg,
    )
    kwargs = chat._relationship_kwargs()
    assert kwargs == {"user_nickname": "小明", "relationship": "哥哥"}


def test_default_config_injects_no_relationship_wording(db):
    """默认配置没填昵称和关系称呼，提示词里就不该出现关系措辞。"""
    chat = ChatService(
        db=db,
        chat_provider=None,
        proactive_provider=None,
        summary_provider=None,
        push_event=lambda *_: None,
        cfg=Config(),
    )
    prompt = build_system_prompt(
        name='测试角色',
        birthday='',
        personality='',
        reply_style='',
        **chat._relationship_kwargs(),
    )
    assert "喊他" not in prompt
