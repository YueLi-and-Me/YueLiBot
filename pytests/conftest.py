"""pytest fixtures for the Python backend test suite."""

from __future__ import annotations

from pathlib import Path

import os
import sqlite3

import pytest

from src.core.db.connection import close_db, open_db
from src.core.db.migrations.manager import run_migrations
from src.core.api.auth import token_manager
from src.core.config.bootstrap import render_example_configs
from src.core.observe.events import reset_for_tests
from src.core.observe.store import event_store

# 模板里 api_key 恒为空——那是唯一必须由用户自己填的东西——而配置加载器与
# WebUI 保存路径都会因此拒绝整份文档。用例需要的是「一份能加载的全新安装配置」。
FAKE_API_KEY = 'sk-test-fixture'


def render_loadable_config(destination: Path) -> Path:
    """渲染一份全新安装会得到的配置目录，并补上假 api_key 使其可加载。

    :param destination: 配置目录落点；不存在时递归创建，已存在的同名文件被覆盖。
    :return: 传入的 ``destination``，便于链式使用。
    副作用：写入四份主 TOML、``adapter.toml`` 与 ``adapters/`` 下的模板。

    多个包过去各自去读仓库根的 ``config/``：
    - 现象：全新签出（含 CI）上该目录不存在，用例在 setup 阶段就 FileNotFoundError。
    - 原因：``config/`` 与 ``adapters/*/config.toml`` 都是不入库的运行时配置。
    - 后果：本机常绿而 CI 必红；更隐蔽的是期望失败的那几条会因为「配置目录缺失」
      而照样通过，验的不是自己声称要验的东西。
    """
    render_example_configs(destination)
    providers = destination / 'providers.toml'
    providers.write_text(
        providers.read_text(encoding='utf-8').replace(
            'api_key = ""', f'api_key = "{FAKE_API_KEY}"',
        ),
        encoding='utf-8',
    )
    return destination


@pytest.fixture(scope="function")
def db() -> sqlite3.Connection:
    """每个测试函数拿到一个隔离的 :memory: 数据库，跑完自动关掉。"""
    # 绕过全局单例，直接构造连接
    import src.core.db.connection as conn_mod
    conn_mod._db = None  # 重置单例

    connection = open_db(":memory:")
    run_migrations(connection, db_path=None)
    yield connection

    close_db()
    conn_mod._db = None


@pytest.fixture(autouse=True)
def set_yueli_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """测试时注入占位 token，避免 auth 模块报错。"""
    token_manager.configure("test-token-fixture")
    monkeypatch.setenv("YUELI_DATA_DIR", "/tmp/yueli-test")
    event_store.configure(tmp_path / "pipeline-events.db")
    reset_for_tests()
    yield
    event_store.close()
    reset_for_tests()
