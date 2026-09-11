"""配置升级的字段增删：带行内注释的段标题与非法 TOML 回滚。"""

from __future__ import annotations

from pathlib import Path

import tomllib

from src.core.config.upgrade import AddedField, apply_added_fields, apply_removed_fields


def test_apply_added_fields_matches_commented_section_header(tmp_path: Path) -> None:
    """段标题带行内注释时补字段进目标段，不新建同名表、不截断值里的 #。"""

    path = tmp_path / 'bot.toml'
    path.write_text(
        '[inner]\n'
        'version = "1.6.0"\n'
        '\n'
        '[schedule] # 每日方向与活动：控制活动能否进入睡眠、方向生成失败时的备用主题与重试节奏。\n'
        'fallback_theme = "按自己的节奏 # 度过今天。"\n'
        'generation_retry_interval_minutes = 10\n'
        '\n'
        '[bot] # 身份与关系\n'
        'name = "月璃"\n',
        encoding='utf-8',
    )

    assert apply_added_fields(
        path, [AddedField('schedule', 'energy_enabled', 'true')],
    ) is True

    text = path.read_text(encoding='utf-8')
    raw = tomllib.loads(text)
    assert raw['schedule']['energy_enabled'] is True
    assert raw['schedule']['fallback_theme'] == '按自己的节奏 # 度过今天。'
    lines = text.splitlines()
    assert sum(1 for line in lines if line.startswith('[schedule]')) == 1
    assert any(line.startswith('energy_enabled') for line in lines)


def test_apply_removed_fields_matches_commented_section_header(tmp_path: Path) -> None:
    """废弃字段能从带行内注释的段标题下删除，并保留其余字段与注释。"""

    path = tmp_path / 'bot.toml'
    path.write_text(
        '[inner]\n'
        'version = "1.5.0"\n'
        '\n'
        '[schedule] # 每日方向与活动\n'
        'sleep_enabled = true\n'
        'fallback_theme = "按自己的节奏 # 度过今天。"\n',
        encoding='utf-8',
    )

    removed = apply_removed_fields(path, ['schedule.sleep_enabled'])

    assert removed == ['schedule.sleep_enabled']
    text = path.read_text(encoding='utf-8')
    raw = tomllib.loads(text)
    assert 'sleep_enabled' not in raw['schedule']
    assert raw['schedule']['fallback_theme'] == '按自己的节奏 # 度过今天。'
    assert '[schedule] # 每日方向与活动' in text


def test_apply_added_fields_reverts_invalid_toml(tmp_path: Path) -> None:
    """新增字段会写出非法 TOML 时不落盘，保留原文等待下次修正。"""

    path = tmp_path / 'bot.toml'
    original = (
        '[inner]\n'
        'version = "1.6.0"\n'
        '\n'
        '[schedule] # 该段不要被破坏\n'
        'fallback_theme = "正常值"\n'
    )
    path.write_text(original, encoding='utf-8')

    assert apply_added_fields(path, [AddedField('schedule', 'broken', '')]) is False

    assert path.read_text(encoding='utf-8') == original
    assert tomllib.loads(path.read_text(encoding='utf-8'))['schedule']['fallback_theme'] == '正常值'
