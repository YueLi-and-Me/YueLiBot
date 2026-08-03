"""
TOML 配置加载 + bot.relationship/user_nickname 确实传进 prompt 的回归。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from yueli.agent.prompt import build_system_prompt
from yueli.config.loader import load_config, reset_config
from yueli.config.schema import Config
from yueli.services.chat import ChatService


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    reset_config()
    yield
    reset_config()


def _write_toml(text: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="yueli_config_test_"))
    path = tmp / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_parses_nested_sections():
    path = _write_toml("""
[bot]
user_nickname = "小明"
relationship = "哥哥"

[llm]
provider = "deepseek"
model = "deepseek-chat"
api_key = "sk-test"

[tts]
enabled = true
base_url = "https://example.com"
model = "tts-1"
voice = "alloy"
""")
    cfg = load_config(path)
    assert cfg.bot.user_nickname == "小明"
    assert cfg.bot.relationship == "哥哥"
    assert cfg.llm.provider == "deepseek"
    assert cfg.llm.api_key == "sk-test"
    assert cfg.tts.ready is True


def test_load_config_defaults_when_sections_missing():
    path = _write_toml("")
    cfg = load_config(path)
    assert cfg == Config()
    assert cfg.tts.ready is False
    assert cfg.vision.ready is False


def test_load_config_exits_on_type_error():
    path = _write_toml("""
[tts]
enabled = "not-a-bool"
""")
    with pytest.raises(SystemExit):
        load_config(path)


def test_prompt_includes_relationship_and_nickname_when_set():
    prompt = build_system_prompt(user_nickname="小明", relationship="哥哥")
    assert "小明" in prompt
    assert "哥哥" in prompt


def test_prompt_omits_relationship_block_when_unset():
    prompt = build_system_prompt()
    # 没传 user_nickname/relationship 时不应该出现"喊他"这类关系措辞
    assert "喊他" not in prompt


def test_chat_service_threads_relationship_into_prompt(db):
    cfg = Config()
    cfg.bot.user_nickname = "小明"
    cfg.bot.relationship = "哥哥"
    chat = ChatService(db=db, provider=None, push_event=lambda *_: None, cfg=cfg)
    kwargs = chat._relationship_kwargs()
    assert kwargs == {"user_nickname": "小明", "relationship": "哥哥"}


def test_chat_service_without_cfg_omits_relationship_kwargs(db):
    chat = ChatService(db=db, provider=None, push_event=lambda *_: None)
    assert chat._relationship_kwargs() == {}
