"""pytest fixtures for the Python backend test suite."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from yueli.common.db.connection import open_db, close_db
from yueli.common.db.migrations.manager import run_migrations


@pytest.fixture(scope="function")
def db() -> sqlite3.Connection:
    """每个测试函数拿到一个隔离的 :memory: 数据库，跑完自动关掉。"""
    # 绕过全局单例，直接构造连接
    import yueli.common.db.connection as conn_mod
    conn_mod._db = None  # 重置单例

    connection = open_db(":memory:")
    run_migrations(connection, db_path=None)
    yield connection

    close_db()
    conn_mod._db = None


@pytest.fixture(autouse=True)
def set_yueli_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试时注入占位 token，避免 auth 模块报错。"""
    monkeypatch.setenv("YUELI_TOKEN", "test-token-fixture")
    monkeypatch.setenv("YUELI_DATA_DIR", "/tmp/yueli-test")
