"""月璃设置 WebUI 配置快照与原子保存回归。"""

from __future__ import annotations

from pathlib import Path
import shutil
import tomllib

import pytest

from src.core.config import adapter_selection, settings_webui

from pytests.conftest import render_loadable_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 主体配置目录内的四份 TOML；适配器连接配置在 adapters/ 下，单独处理。
_MAIN_FILES = ('bot.toml', 'features.toml', 'providers.toml', 'models.toml')
_ADAPTER_PLUGIN = 'yueli-snowluma-adapter'


@pytest.fixture()
def config_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """造一套全新安装形态的配置，并把适配器根目录改指到临时副本。

    :param tmp_path: pytest 提供的临时目录。
    :param monkeypatch: 用于改指适配器根目录常量。
    :return: 主体配置目录。

    适配器根目录是 ``adapter_selection`` 的模块常量（由文件位置推出仓库根）。
    不改指它，保存用例就会直接写工作区里那份真实的适配器连接配置。

    配置来自模板渲染而不是拷贝仓库根的 ``config/``：那个目录与
    ``adapters/*/config.toml`` 都不入库，全新签出（含 CI）上并不存在。
    插件的 ``_manifest.json`` 与源码入库，直接复用真实的那份。
    """
    config_dir = render_loadable_config(tmp_path / 'config')

    adapters_root = tmp_path / 'adapters'
    plugin_dir = adapters_root / _ADAPTER_PLUGIN
    shutil.copytree(
        PROJECT_ROOT / 'adapters' / _ADAPTER_PLUGIN,
        plugin_dir,
        ignore=shutil.ignore_patterns('__pycache__', 'config.toml'),
    )
    # 连接配置取模板渲染出的那份，与全新安装拿到的逐字一致。
    shutil.copyfile(
        config_dir / 'adapters' / f'{_ADAPTER_PLUGIN}.toml',
        plugin_dir / 'config.toml',
    )
    monkeypatch.setattr(adapter_selection, 'ADAPTERS_ROOT', adapters_root)
    return config_dir


def _adapter_config(config_dir: Path) -> Path:
    """给出该临时环境里适配器连接配置的路径。"""
    return adapter_selection.adapter_config_path(
        adapter_selection.read_active_adapter(config_dir)
    )


def test_snapshot_covers_five_files_and_masks_keys(config_copy: Path) -> None:
    snap = settings_webui.snapshot(config_copy)
    assert [item['file'] for item in snap['schema']['files']] == [
        'bot.toml', 'features.toml', 'adapter.toml', 'providers.toml', 'models.toml',
    ]
    assert set(snap['values']) == {
        'bot.toml', 'features.toml', 'adapter.toml', 'providers.toml', 'models.toml',
    }
    adapter_schema = next(
        item for item in snap['schema']['files'] if item['file'] == 'adapter.toml'
    )
    # 逻辑标识不等于磁盘文件名，说明里必须给出真正写的是哪份文件。
    assert str(_adapter_config(config_copy)) in adapter_schema['description']
    bot_schema = next(item for item in snap['schema']['files'] if item['file'] == 'bot.toml')
    agent_schema = next(
        section for section in bot_schema['sections']
        if section['key'] == 'conversation_agent'
    )
    assert any(field['key'] == 'tool_calling' for field in agent_schema['fields'])
    providers = snap['values']['providers.toml']['api_providers']
    assert providers
    assert all(item.get('api_key') == '' for item in providers)


def test_save_round_trip_keeps_values_and_writes_comments(config_copy: Path) -> None:
    before = settings_webui.snapshot(config_copy)
    result = settings_webui.save(config_copy, before['values'])
    assert result['ok'] is True
    after = settings_webui.snapshot(config_copy)
    assert before['values'] == after['values']
    for name in _MAIN_FILES:
        assert '# ' in (config_copy / name).read_text(encoding='utf-8')
    adapter_text = _adapter_config(config_copy).read_text(encoding='utf-8')
    assert '# ' in adapter_text
    # 连接段名取自适配器清单，不是模型字段名。
    assert '[snowluma]' in adapter_text
    assert '[napcat]' not in adapter_text


def test_save_rejects_empty_bot_name_without_touching_files(config_copy: Path) -> None:
    snap = settings_webui.snapshot(config_copy)
    original = {
        path: path.read_bytes()
        for path in [config_copy / name for name in _MAIN_FILES] + [_adapter_config(config_copy)]
    }
    snap['values']['bot.toml']['bot']['name'] = ''
    result = settings_webui.save(config_copy, snap['values'])
    assert result['ok'] is False
    assert 'bot.name' in result['detail']
    for path, content in original.items():
        assert path.read_bytes() == content


def test_save_keeps_newly_added_task_slots(config_copy: Path) -> None:
    """设置页保存不能清掉新增模型槽的配置。

    写盘是 schema 驱动的，只写 entries 里列出的键；漏登记时那一整段会在保存后
    消失，而且不报错——已经配好的候选被清空，看起来像「没保存成功」。
    """
    before = settings_webui.snapshot(config_copy)
    # 候选名从当前配置里现取，不写死某个具体模型：保存路径会校验候选必须已定义，
    # 写死的名字会把用例绑在某一份配置上，换一份种子就红。
    available = [entry['name'] for entry in before['values']['models.toml']['models']]
    assert available, '配置里没有任何模型条目，用例前提不成立'
    chosen = available[-1]

    tasks = before['values']['models.toml']['model_tasks']
    for slot in ('planner', 'replyer', 'scene'):
        tasks[slot]['model_list'] = [chosen]
        tasks[slot]['selection_strategy'] = 'random'

    assert settings_webui.save(config_copy, before['values'])['ok'] is True

    after = settings_webui.snapshot(config_copy)['values']['models.toml']['model_tasks']
    for slot in ('planner', 'replyer', 'scene'):
        assert after[slot]['model_list'] == [chosen], slot
        assert after[slot]['selection_strategy'] == 'random', slot


def test_balance_strategy_is_exposed_and_round_trips(config_copy: Path) -> None:
    snapshot = settings_webui.snapshot(config_copy)
    models_schema = next(
        item for item in snapshot['schema']['files'] if item['file'] == 'models.toml'
    )
    task_schema = next(
        section for section in models_schema['sections'] if section['key'] == 'model_tasks'
    )
    strategy_field = next(
        field for field in task_schema['fields'] if field['key'] == 'selection_strategy'
    )
    assert any(option['value'] == 'balance' for option in strategy_field['options'])

    tasks = snapshot['values']['models.toml']['model_tasks']
    tasks['chat']['selection_strategy'] = 'balance'
    assert settings_webui.save(config_copy, snapshot['values'])['ok'] is True

    restored = settings_webui.snapshot(config_copy)['values']['models.toml']['model_tasks']
    assert restored['chat']['selection_strategy'] == 'balance'


def test_schema_entries_must_match_config_model() -> None:
    """schema 的任务条目与配置模型不一致时必须在加载期失败，而不是静默丢配置。"""
    from src.core.config.settings_webui import _require_task_entries_match_config

    broken = {
        'files': [{
            'file': 'models.toml',
            'sections': [{'key': 'model_tasks', 'kind': 'map', 'entries': [{'key': 'chat'}]}],
        }],
    }

    with pytest.raises(ValueError, match='与配置模型不一致'):
        _require_task_entries_match_config(broken)


def test_snapshot_values_cover_every_schema_section_key(config_copy: Path) -> None:
    """快照必须按 schema 段键供数，含 ``typing.nudge`` 这类点号嵌套段。

    前端按段键平铺索引 ``values[file][key]``；点号段若只存在于模型导出的嵌套
    路径里，面板取不到值，布尔开关恒渲染为关、数字框恒渲染为空。
    """
    snap = settings_webui.snapshot(config_copy)
    missing = [
        f"{item['file']} [{section['key']}]"
        for item in settings_webui.load_schema()['files']
        for section in item.get('sections', [])
        if section.get('key', '') not in snap['values'].get(item['file'], {})
    ]
    assert not missing, '快照缺少这些段键：' + ', '.join(missing)


def _flip_nudge_enabled(values: dict, enabled: bool) -> None:
    """模拟前端的平铺编辑：按段键整段改写 ``values['bot.toml']['typing.nudge']``。"""
    bot_values = values['bot.toml']
    section = dict(bot_values.get('typing.nudge') or {})
    section['enabled'] = enabled
    bot_values['typing.nudge'] = section


def _disk_bot_document(config_dir: Path) -> dict:
    """从磁盘原样读回 bot.toml，绕过快照，直接核对写盘结果。"""
    return tomllib.loads((config_dir / 'bot.toml').read_text(encoding='utf-8'))


def test_nested_section_switch_round_trips_through_save(config_copy: Path) -> None:
    """点号嵌套段的开关经设置页保存后必须落到磁盘，且能改回来。

    前端按段键平铺读写，开关翻转提交的是平铺的 ``typing.nudge`` 段值；写侧
    若只认模型导出的嵌套旧值，这次编辑会被静默丢弃——文件里仍是保存前的值。
    """
    before = settings_webui.snapshot(config_copy)
    follow_up_before = before['values']['bot.toml']['typing']['follow_up']['enabled']
    cleanup_before = before['values']['bot.toml']['emoji']['cleanup']['enabled']

    _flip_nudge_enabled(before['values'], False)
    assert settings_webui.save(config_copy, before['values'])['ok'] is True
    document = _disk_bot_document(config_copy)
    assert document['typing']['nudge']['enabled'] is False
    # 不相干的另两个点号段不能被这次保存顺手改掉。
    assert document['typing']['follow_up']['enabled'] is follow_up_before
    assert document['emoji']['cleanup']['enabled'] is cleanup_before

    after = settings_webui.snapshot(config_copy)
    _flip_nudge_enabled(after['values'], True)
    assert settings_webui.save(config_copy, after['values'])['ok'] is True
    document = _disk_bot_document(config_copy)
    assert document['typing']['nudge']['enabled'] is True
    assert document['typing']['follow_up']['enabled'] is follow_up_before
    assert document['emoji']['cleanup']['enabled'] is cleanup_before
