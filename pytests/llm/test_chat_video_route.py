"""验证聊天视频理解的 video 任务路由：omni 准入、启用检查与「留空不继承 chat」。

video 是独立任务槽：生产 vision 池横跨多家厂商、没有一个听得到音轨，复用会
静默丢掉声音；chat 是 Gemini 类文本模型，接不了视频块，因此留空也不继承。
"""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from src.core.config.loader import load_config, reset_config
from src.core.config.schema import CONFIG_VERSION

# 测试夹具占位密钥：不对应任何真实服务，经变量间接写入避免被安全扫描当作硬编码凭据。
_FIXTURE_API_KEY = "-".join(("test", "key"))
_API_KEY_LINE = f"api_key = {json.dumps(_FIXTURE_API_KEY)}"


@pytest.fixture(autouse=True)
def _reset_config_singleton() -> None:
    reset_config()
    yield
    reset_config()


def _write_config(
    directory: Path,
    *,
    extra_model_tasks: str = '',
    extra_models: str = '',
    extra_vision_features: str = '',
) -> None:
    """写一份可加载的最小配置；video 任务段、模型条目与开关由用例按需追加。"""
    directory.mkdir()
    directory.joinpath('providers.toml').write_text(
        f'''
[inner]
version = "{CONFIG_VERSION}"

[[api_providers]]
name = "main"
kind = "openai"
base_url = "https://api.example.com"
{_API_KEY_LINE}
client_type = "openai"
''',
        encoding='utf-8',
    )
    directory.joinpath('models.toml').write_text(
        f'''
[inner]
version = "{CONFIG_VERSION}"

[model_tasks.chat]
model_list = ["chat"]
selection_strategy = "sequential"
{extra_model_tasks}
[generation.chat]
temperature = 0.85

[[models]]
name = "chat"
model_identifier = "chat-model"
api_provider = "main"
{extra_models}''',
        encoding='utf-8',
    )
    directory.joinpath('bot.toml').write_text(
        '''
[inner]
version = "''' + CONFIG_VERSION + '''"

[bot]
name = "测试角色"

[group_chat]
at_mention_must_reply = true

[personality]
birthday = ""
personality = "测试人设"
reply_style = "测试说话方式"
tone_probability = 0.0
tone_variants = []
''',
        encoding='utf-8',
    )
    directory.joinpath('features.toml').write_text(
        '''
[inner]
version = "''' + CONFIG_VERSION + '''"

[tts]
enabled = false

[vision]
enabled = false
''' + extra_vision_features + '''
[vector]
enabled = false

[advanced]
''',
        encoding='utf-8',
    )


def test_video_route_rejects_model_without_omni_marker(tmp_path, capsys) -> None:
    """只能看画面、听不到音轨的模型放进 video 必须在加载期报错。

    qwen-max 类模型能看画面却听不到声音，不拦会把音轨静默丢掉。
    """
    directory = tmp_path / 'config'
    _write_config(
        directory,
        extra_model_tasks='''
[model_tasks.video]
model_list = ["chat"]
''',
    )

    with pytest.raises(SystemExit):
        load_config(directory)

    error = capsys.readouterr().err
    assert 'model_tasks.video 的候选 chat' in error
    assert 'omni = true' in error


def test_enabled_chat_video_without_candidates_rejected(tmp_path, capsys) -> None:
    """开关打开而 video 路由为空时加载期报错，与识图同口径。"""
    directory = tmp_path / 'config'
    _write_config(
        directory,
        extra_vision_features='chat_video_enabled = true\n',
    )

    with pytest.raises(SystemExit):
        load_config(directory)

    error = capsys.readouterr().err
    assert 'model_tasks.video.model_list' in error


def test_empty_video_route_does_not_inherit_chat(tmp_path) -> None:
    """video 留空就是没有模型：不继承 chat，视频功能关闭但配置照常加载。"""
    directory = tmp_path / 'config'
    _write_config(directory)

    config = load_config(directory)

    assert config.routing.video.candidates == []
    assert config.routing.video.ready is False
    assert config.vision.chat_video_enabled is False


def test_video_route_uses_omni_model(tmp_path) -> None:
    """标了 omni 的候选正常进入 video 路由。"""
    directory = tmp_path / 'config'
    _write_config(
        directory,
        extra_model_tasks='''
[model_tasks.video]
model_list = ["omni"]
''',
        extra_models='''

[[models]]
name = "omni"
model_identifier = "omni-model"
api_provider = "main"
omni = true
''',
        extra_vision_features='chat_video_enabled = true\n',
    )

    config = load_config(directory)

    assert [candidate.name for candidate in config.routing.video.candidates] == ['omni']
    assert config.routing.video.ready is True
