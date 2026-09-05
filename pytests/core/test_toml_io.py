"""版本化 TOML 读取的独立回归测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config.toml_io import read_versioned_toml


def test_read_versioned_toml_returns_complete_document(tmp_path: Path) -> None:
    path = tmp_path / 'config.toml'
    path.write_text(
        '[inner]\nversion = "1.1.0"\n\n[bot]\nname = "月璃"\n',
        encoding='utf-8',
    )

    document = read_versioned_toml(path, '1.1.0', '请更新配置')

    assert document == {'inner': {'version': '1.1.0'}, 'bot': {'name': '月璃'}}


def test_read_versioned_toml_reports_actual_expected_version_and_hint(tmp_path: Path) -> None:
    path = tmp_path / 'config.toml'
    path.write_text('[inner]\nversion = "1.0.0"\n', encoding='utf-8')
    hint = '请按模板补齐配置'

    with pytest.raises(ValueError) as exc_info:
        read_versioned_toml(path, '1.1.0', hint)

    message = str(exc_info.value)
    assert "'1.0.0'" in message
    assert '1.1.0' in message
    assert hint in message


def test_read_versioned_toml_rejects_missing_inner_section(tmp_path: Path) -> None:
    path = tmp_path / 'config.toml'
    path.write_text('[bot]\nname = "月璃"\n', encoding='utf-8')

    with pytest.raises(ValueError) as exc_info:
        read_versioned_toml(path, '1.1.0', '请更新配置')

    assert 'None' in str(exc_info.value)
