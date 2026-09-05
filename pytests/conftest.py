"""pytest fixtures for the Python backend test suite."""

from __future__ import annotations

from pathlib import Path

import os
import sqlite3

import pytest

from src.core.db.connection import close_db, open_db
from src.core.db.migrations.manager import run_migrations
from src.core.api.auth import token_manager
from src.core.observe.events import reset_for_tests
from src.core.observe.store import event_store


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
