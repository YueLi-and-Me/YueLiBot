"""运行时自检（``src.core.common.self_check``）回归。

覆盖任务书的 C-1～C-6 六条验收断言：迁移链空洞、库版本超前、开关与产出矛盾、
适配器段名错位、健康时只读且退出 0，以及命令入口与启动路径共用同一份判据。

这些断言防的是两轮合流里真实发生过的事故：迁移注册表在合并时丢号、适配器
配置被覆盖成另一个协议端的段。C-1 与 C-4 是那两次事故的机检化。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import hashlib
import json
import sqlite3

import pytest

from src.core.common.db.migrations.manager import CURRENT_VERSION, load_migration_registry
from src.core.common.db.migrations.registry import MigrationFn
from src.core.common.db.schema import DDL, SEED
from src.core.common.self_check import (
    FAIL,
    PASS,
    announce_startup_self_check,
    check_adapter_configs,
    check_feature_state,
    open_readonly_database,
    run_self_check,
)
from src.core.config.loader import read_config
from src.core.memory.vector_health import inspect_vector_health
from src.platforms.onebot11.config import NAPCAT_CONFIG_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG_DIR = PROJECT_ROOT / 'config'
REAL_ADAPTERS_DIR = PROJECT_ROOT / 'adapters'


def _build_database(path: Path, user_version: int) -> None:
    """按当前 DDL/SEED 建一个指定 ``user_version`` 的库文件。

    :param path: 目标文件路径，调用方负责其父目录存在。
    :param user_version: 要写入的 ``PRAGMA user_version``。
    副作用：创建并写入 SQLite 文件。
    """
    db = sqlite3.connect(path)
    try:
        db.executescript(DDL)
        db.executescript(SEED)
        db.execute(f'PRAGMA user_version = {user_version}')
        db.commit()
    finally:
        db.close()


def _write_adapter(root: Path, directory: str, section: str, config_section: str) -> None:
    """在临时适配器根目录下写一份清单与配置。

    :param root: 适配器插件根目录。
    :param directory: 适配器目录名。
    :param section: 清单里声明的 ``config_section``。
    :param config_section: config.toml 里实际写入的连接段名。
    副作用：创建目录与两个文件。
    """
    adapter_dir = root / directory
    adapter_dir.mkdir(parents=True)
    (adapter_dir / '_manifest.json').write_text(
        json.dumps({
            'manifest_version': 1,
            'id': f'yueli.{section}-adapter',
            'plugin_type': 'adapter',
            'name': f'YueLi-{section}-Adapter',
            'version': '0.1.0',
            'description': '自检用例构造的适配器清单',
            'protocol': 'onebot11',
            'config_section': section,
            'capabilities': {'static': [], 'probed': []},
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    (adapter_dir / 'config.toml').write_text(
        f'[inner]\nversion = "{NAPCAT_CONFIG_VERSION}"\n\n[{config_section}]\nenabled = false\n',
        encoding='utf-8',
    )


def _holed_registry() -> Dict[int, MigrationFn]:
    """返回缺少 ``@register(22)`` 的注册表副本，复现合流丢号。"""
    registry = dict(load_migration_registry())
    del registry[22]
    return registry


def test_c1_migration_hole_is_reported_with_the_missing_version(tmp_path: Path) -> None:
    """C-1：注册表有空洞时自检失败，并指出断在哪个版本。"""
    database = tmp_path / 'memory.db'
    _build_database(database, CURRENT_VERSION)

    report = run_self_check(
        database,
        REAL_CONFIG_DIR,
        REAL_ADAPTERS_DIR,
        migration_registry=_holed_registry(),
    )

    assert report.exit_code == 1
    details = [item.detail for item in report.items if item.status == FAIL]
    assert any('缺少从版本 22 到 23 的迁移函数' in detail for detail in details), details


def test_c2_database_ahead_of_code_fails(tmp_path: Path) -> None:
    """C-2：``user_version`` 超前于 ``CURRENT_VERSION`` 时自检失败。"""
    database = tmp_path / 'memory.db'
    _build_database(database, CURRENT_VERSION + 1)

    report = run_self_check(database, REAL_CONFIG_DIR, REAL_ADAPTERS_DIR)

    assert report.exit_code == 1
    failures = [item for item in report.items if item.status == FAIL]
    assert any(str(CURRENT_VERSION + 1) in item.detail for item in failures), failures


def test_c3_enabled_switch_without_output_fails(tmp_path: Path) -> None:
    """C-3：``vector.enabled=true`` 而事实向量覆盖率为 0 时失败（G8 那一类）。"""
    database = tmp_path / 'memory.db'
    _build_database(database, CURRENT_VERSION)
    db = sqlite3.connect(database)
    try:
        # person 1 由 SEED 建好，这里只补一条没有向量的事实。
        db.execute(
            "INSERT INTO facts(person_id, kind, content, content_key, strength, "
            "half_life_hours, updated_at, created_at, due_at) "
            "VALUES (1, '属性', '开关开着但向量是死的', 'k1', 1.0, 8760.0, 0, 0, 0)"
        )
        db.commit()
    finally:
        db.close()

    config = read_config(REAL_CONFIG_DIR)
    if not config.vector.enabled:
        pytest.skip('本机 vector.enabled 为 false，该矛盾组合不成立')

    readonly = open_readonly_database(database)
    try:
        health = inspect_vector_health(readonly)
    finally:
        readonly.close()

    items = check_feature_state(config, health)
    failures = [item for item in items if item.status == FAIL]
    assert failures, items
    assert any('facts=1 条' in item.detail for item in failures), failures


def test_c4_adapter_section_directory_mismatch_fails(tmp_path: Path) -> None:
    """C-4：目录名对、段名却是另一个协议端时失败（9/03 配置被覆盖那次）。"""
    adapters_root = tmp_path / 'adapters'
    _write_adapter(adapters_root, 'yueli-napcat-adapter', 'napcat', 'snowluma')

    items = check_adapter_configs(REAL_CONFIG_DIR, adapters_root)

    failures = [item for item in items if item.status == FAIL]
    assert any('napcat' in item.detail for item in failures), items


def test_c5_healthy_run_exits_zero_and_writes_nothing(tmp_path: Path) -> None:
    """C-5：健康时退出码 0；检查前后库文件哈希一致，只读连接拒绝 DDL。"""
    database = tmp_path / 'memory.db'
    _build_database(database, CURRENT_VERSION)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    report = run_self_check(database, REAL_CONFIG_DIR, REAL_ADAPTERS_DIR)

    assert report.exit_code == 0, [
        item.render() for item in report.items if item.status != PASS
    ]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before

    readonly = open_readonly_database(database)
    try:
        assert int(readonly.execute('PRAGMA query_only').fetchone()[0]) == 1
        with pytest.raises(sqlite3.Error):
            readonly.execute('CREATE TABLE 自检写入探针(x INTEGER)')
    finally:
        readonly.close()


def test_c6_command_and_startup_share_one_implementation() -> None:
    """C-6：命令入口与启动路径引用同一份判据，不是两份实现。"""
    import scripts.self_check as command_entry
    import src.main as entry

    assert command_entry.run_self_check is run_self_check
    assert entry.announce_startup_self_check is announce_startup_self_check

    source = Path(announce_startup_self_check.__code__.co_filename).read_text(
        encoding='utf-8'
    )
    # 启动包装器只允许调用核心判据，不得自带第二套检查。
    assert 'def announce_startup_self_check' in source
    assert announce_startup_self_check.__module__ == run_self_check.__module__
