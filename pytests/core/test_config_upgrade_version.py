"""配置升级的端到端路径：旧版目录就地升级后仍是合法 TOML 且能重新加载。"""

from __future__ import annotations

from pathlib import Path

import tomllib

import pytest

from pytests.conftest import render_loadable_config
from src.core.config.loader import load_config, reset_config
from src.core.config.schema import (
    CONFIG_VERSION,
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


def _downgrade_to_legacy(directory: Path) -> None:
    """把全新 1.6.0 模板改造成真机那种 1.5.0 + sleep_enabled 的旧配置。"""

    for name in _DOCUMENTS:
        path = directory / name
        text = path.read_text(encoding='utf-8')
        assert f'version = "{CONFIG_VERSION}"' in text
        path.write_text(
            text.replace(f'version = "{CONFIG_VERSION}"', 'version = "1.5.0"', 1),
            encoding='utf-8',
        )
    bot = directory / 'bot.toml'
    text = bot.read_text(encoding='utf-8')
    assert 'energy_enabled = true' in text
    bot.write_text(text.replace('energy_enabled = true', 'sleep_enabled = true', 1), encoding='utf-8')


def _line_span(lines: list[str], section: str) -> tuple[int, int]:
    """返回段标题行到下一个段标题之间的半开区间，供断言字段插入位置。"""

    start = next(i for i, line in enumerate(lines) if line.startswith(f'[{section}]'))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')),
        len(lines),
    )
    return start, end


def test_legacy_directory_upgrades_and_reloads_as_toml(tmp_path: Path) -> None:
    """真机部署路径：1.5.0 有注释段标题的配置跑完升级后仍合法且能 load_config。"""

    directory = render_loadable_config(tmp_path / 'config')
    _downgrade_to_legacy(directory)
    data_dir = tmp_path / 'data'
    data_dir.mkdir()

    diffs = upgrade_config_directory(directory, _DOCUMENTS, data_dir)

    for name in _DOCUMENTS:
        parsed = tomllib.loads((directory / name).read_text(encoding='utf-8'))
        assert parsed['inner']['version'] == CONFIG_VERSION
    bot_text = (directory / 'bot.toml').read_text(encoding='utf-8')
    bot_raw = tomllib.loads(bot_text)
    assert bot_raw['schedule']['energy_enabled'] is True
    assert 'sleep_enabled' not in bot_raw['schedule']
    lines = bot_text.splitlines()
    assert sum(1 for line in lines if line.startswith('[schedule]')) == 1
    schedule_start, schedule_end = _line_span(lines, 'schedule')
    energy_lines = [
        index for index in range(schedule_start, schedule_end)
        if lines[index].startswith('energy_enabled')
    ]
    assert len(energy_lines) == 1, '新增字段必须落在 [schedule] 段内部'

    config = load_config(directory)
    assert config.schedule.energy_enabled is True
    assert list((data_dir / 'backups' / 'config').iterdir()), '写入前必须留下整目录备份'

    bot_diff = next(diff for diff in diffs if diff.name == 'bot.toml')
    assert bot_diff.version_from == '1.5.0'
    assert bot_diff.version_to == CONFIG_VERSION
    assert bot_diff.version_upgraded() is True
    provider_diff = next(diff for diff in diffs if diff.name == 'providers.toml')
    assert provider_diff.added == [] and provider_diff.removed == []
    assert provider_diff.version_upgraded() is True
