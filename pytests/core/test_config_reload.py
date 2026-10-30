"""配置文件自动重载、无效文件修正与服务应用失败的回归验证。"""

from pathlib import Path
from typing import Iterator

import asyncio
import os
import re
import tomllib

import pytest

from pytests.conftest import render_loadable_config
from src.core.config import loader
from src.core.config.watcher import ConfigWatcher


@pytest.fixture()
def config_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """隔离配置单例、订阅者与文件，避免改动实际运行配置。"""
    loader.reset_config()
    monkeypatch.setattr(loader, '_reload_listeners', [])
    directory = render_loadable_config(tmp_path / 'config')
    loader.load_config(directory)
    yield directory
    loader.reset_config()


def _set_probability(directory: Path, value: float) -> None:
    path = directory / 'bot.toml'
    text, count = re.subn(
        r'^tone_probability = [^\n]*', f'tone_probability = {value}',
        path.read_text(encoding='utf-8'), flags=re.MULTILINE,
    )
    assert count == 1
    path.write_text(text, encoding='utf-8')


async def test_file_change_applies_after_stable_save(config_directory: Path) -> None:
    original = loader.get_config()
    calls = []
    loader.add_config_reload_listener(lambda previous, fresh: calls.append((previous, fresh)))
    watcher = ConfigWatcher(config_directory)
    _set_probability(config_directory, 0.37)

    assert await watcher.check_once() is None
    assert loader.get_config() is original
    fresh, fields = await watcher.check_once()

    assert fresh.personality.tone_probability == 0.37
    assert fields == ['personality.tone_probability']
    assert calls == [(original, fresh)]
    assert await watcher.check_once() is None
    assert len(calls) == 1


async def test_content_change_detected_with_unchanged_mtime_and_size(config_directory: Path) -> None:
    path = config_directory / 'bot.toml'
    # 先规范化成没有注释的一行，下一次只替换同样长度的数字。
    _set_probability(config_directory, 0.1)
    loader.reload_config()
    watcher = ConfigWatcher(config_directory)
    before = path.stat()
    _set_probability(config_directory, 0.2)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size

    await watcher.check_once()
    await watcher.check_once()

    assert loader.get_config().personality.tone_probability == 0.2


async def test_invalid_save_reports_error_and_recovers_after_correction(config_directory: Path) -> None:
    original = loader.get_config()
    watcher = ConfigWatcher(config_directory)
    path = config_directory / 'bot.toml'
    valid = path.read_text(encoding='utf-8')
    path.write_text('invalid = [', encoding='utf-8')
    await watcher.check_once()

    with pytest.raises(tomllib.TOMLDecodeError):
        await watcher.check_once()
    assert loader.get_config() is original
    assert await watcher.check_once() is None

    path.write_text(valid, encoding='utf-8')
    _set_probability(config_directory, 0.41)
    await watcher.check_once()
    await watcher.check_once()
    assert loader.get_config().personality.tone_probability == 0.41


async def test_background_watcher_updates_without_restart(config_directory: Path) -> None:
    watcher = ConfigWatcher(config_directory, interval_s=0.01)
    await watcher.startup()
    try:
        _set_probability(config_directory, 0.43)
        async with asyncio.timeout(3):
            while loader.get_config().personality.tone_probability != 0.43:
                await asyncio.sleep(0.01)
    finally:
        await watcher.shutdown()
    assert loader.get_config().personality.tone_probability == 0.43


def test_failed_subscriber_is_reported_and_other_subscribers_still_run(config_directory: Path) -> None:
    calls = []

    def broken(previous, fresh) -> None:
        raise RuntimeError('测试服务应用失败')

    loader.add_config_reload_listener(broken)
    loader.add_config_reload_listener(lambda previous, fresh: calls.append(fresh))
    _set_probability(config_directory, 0.47)

    with pytest.raises(loader.ConfigApplicationError, match='第 1 个配置订阅服务'):
        loader.reload_config()
    assert calls == [loader.get_config()]


async def test_manual_reload_reports_service_application_failure(config_directory: Path) -> None:
    from fastapi import HTTPException
    from src.core.api.http import system_config_reload

    def broken(previous, fresh) -> None:
        raise RuntimeError('测试服务应用失败')

    loader.add_config_reload_listener(broken)
    with pytest.raises(HTTPException) as caught:
        await system_config_reload()
    assert caught.value.status_code == 500
    assert '未完整生效' in caught.value.detail
