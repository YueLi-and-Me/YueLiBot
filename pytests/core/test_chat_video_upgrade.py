"""旧配置升级的视频理解路径：已有 [vision] 段自动补字段并报告，video 子段缺失照常加载。

升级器只往已有的段里补新字段；`model_tasks.video` 这类整段新增不补也不报，
由 schema 默认值兜底——老配置升级后视频理解保持关闭，要在 WebUI 手动打开。
用例一律渲染到临时目录，不读取本机 ``config/``。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

from pytests.conftest import render_loadable_config
from src.core.config.loader import load_config, reset_config
from src.core.config.schema import (
    BotDocument,
    FeatureDocument,
    ModelCatalog,
    ProviderCatalog,
)
from src.core.config.upgrade import upgrade_config_directory

_DOCUMENTS = {
    'providers.toml': ProviderCatalog,
    'models.toml': ModelCatalog,
    'bot.toml': BotDocument,
    'features.toml': FeatureDocument,
}


@pytest.fixture(autouse=True)
def _reset_config_singleton():
    reset_config()
    yield
    reset_config()


def _drop_section(text: str, header: str) -> str:
    """从 TOML 文本里整段移除一个表段，供模拟「这段还不存在」的旧配置。"""
    lines = text.splitlines()
    kept: list[str] = []
    inside = False
    for line in lines:
        if line.lstrip().startswith('['):
            inside = line.split('#', 1)[0].strip() == header
            if inside:
                continue
        if not inside:
            kept.append(line)
    return '\n'.join(kept) + '\n'


def _downgrade_to_pre_chat_video(directory: Path) -> None:
    """把全新模板改造成还没有视频理解的旧配置：[vision] 少三字段、无 video 子段。"""
    features = directory / 'features.toml'
    lines = features.read_text(encoding='utf-8').splitlines()
    kept = [
        line for line in lines
        if not line.lstrip().startswith(
            ('chat_video_enabled', 'chat_video_scope', 'chat_video_max_seconds')
        )
    ]
    features.write_text('\n'.join(kept) + '\n', encoding='utf-8')
    models = directory / 'models.toml'
    text = models.read_text(encoding='utf-8')
    for header in ('[model_tasks.video]', '[generation.video]'):
        text = _drop_section(text, header)
    models.write_text(text, encoding='utf-8')


def test_legacy_vision_section_gains_chat_video_fields_with_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """旧 features.toml 的 [vision] 段启动时自动补齐三字段，控制台报告逐条列出。"""
    directory = render_loadable_config(tmp_path / 'config')
    _downgrade_to_pre_chat_video(directory)
    data_dir = tmp_path / 'data'
    data_dir.mkdir()

    upgrade_config_directory(directory, _DOCUMENTS, data_dir)

    parsed = tomllib.loads((directory / 'features.toml').read_text(encoding='utf-8'))
    # 补进去的是 schema 默认值：老配置升级后保持关闭，要在 WebUI 手动打开。
    assert parsed['vision']['chat_video_enabled'] is False
    assert parsed['vision']['chat_video_scope'] == 'related'
    assert parsed['vision']['chat_video_max_seconds'] == 180
    out = capsys.readouterr().out
    assert 'vision.chat_video_enabled' in out
    assert 'vision.chat_video_scope' in out
    assert 'vision.chat_video_max_seconds' in out


def test_models_toml_without_video_task_loads_with_feature_off(tmp_path: Path) -> None:
    """没有 ``model_tasks.video`` 的 models.toml 照常加载，视频功能关闭。"""
    directory = render_loadable_config(tmp_path / 'config')
    _downgrade_to_pre_chat_video(directory)
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    upgrade_config_directory(directory, _DOCUMENTS, data_dir)

    config = load_config(directory)

    assert config.routing.video.candidates == []
    assert config.routing.video.ready is False
    assert config.vision.chat_video_enabled is False
