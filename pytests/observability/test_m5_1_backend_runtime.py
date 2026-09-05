"""验证后端运行时连接文件的生成、读取和显式令牌认证。

本模块属于后端运行时凭证测试，确认 create_backend_runtime 写入固定结构的 backend.json，
read_backend_runtime 能还原同一对象，并验证认证模块使用当前进程显式配置的令牌。
"""
from __future__ import annotations

from pathlib import Path

import json
import os
import pytest
import shutil
import stat

from src.core.api.auth import token_manager
from src.core.common.backend_runtime import create_backend_runtime, read_backend_runtime


def test_python_creates_and_loads_backend_runtime(tmp_path: Path) -> None:
    """Python 生成令牌，并把适配器所需连接信息写入运行时文件。"""
    runtime = create_backend_runtime(tmp_path, 7999)
    runtime_path = tmp_path / 'runtime' / 'backend.json'

    assert runtime.port == 7999
    assert len(runtime.token) == 64
    assert runtime.token != 'test-token-fixture'
    assert read_backend_runtime(runtime_path) == runtime
    if os.name != 'nt':
        assert stat.S_IMODE(runtime_path.stat().st_mode) & 0o077 == 0
    assert json.loads(runtime_path.read_text('utf-8')) == {
        'port': 7999,
        'token': runtime.token,
    }


def test_auth_uses_explicit_token_manager() -> None:
    """主体认证读取当前进程显式初始化的 token。"""
    token_manager.configure('a' * 64)

    assert token_manager.get() == 'a' * 64
    assert token_manager.verify('a' * 64)
    assert not token_manager.verify('b' * 64)


def test_windows_runtime_directory_stays_deletable(tmp_path: Path) -> None:
    """Windows 下收紧 ACL 后当前用户仍能枚举并删除运行时目录。

    回归用例：若 ACL 授给了名称解析得到的错误主体（同名计算机账户的 SID），
    iterdir 与 rmtree 都会抛 PermissionError，目录只能靠提权 takeown 删除，
    每跑一次测试就在磁盘上留下一个删不掉的临时目录。
    """
    if os.name != 'nt':
        pytest.skip('该用例验证 Windows ACL 行为')

    create_backend_runtime(tmp_path, 7998)
    runtime_dir = tmp_path / 'runtime'

    assert [entry.name for entry in runtime_dir.iterdir()] == ['backend.json']

    shutil.rmtree(runtime_dir)
    assert not runtime_dir.exists()
