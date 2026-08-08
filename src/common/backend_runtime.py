"""主体后端与本机消费者共享的运行时连接信息。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import json
import os
import secrets
import subprocess


@dataclass(frozen=True)
class BackendRuntime:
    """一个正在运行的主体后端的连接坐标。"""

    port: int
    token: str


def create_backend_runtime(data_dir: Path, port: int) -> BackendRuntime:
    """生成进程级 token，并以仅当前用户可读的权限原子写入磁盘。"""
    if port <= 0 or port > 65535:
        raise ValueError('主体 backend 端口必须在 1 到 65535 之间')

    runtime = BackendRuntime(port=port, token=secrets.token_hex(32))
    runtime_dir = data_dir / 'runtime'
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _restrict_runtime_directory(runtime_dir)

    runtime_path = runtime_dir / 'backend.json'
    temporary_path = runtime_dir / f'.backend-{secrets.token_hex(8)}.tmp'
    payload = json.dumps(
        {'port': runtime.port, 'token': runtime.token},
        ensure_ascii=False,
        separators=(',', ':'),
    )
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as file:
            file.write(payload)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, runtime_path)
        if os.name != 'nt':
            os.chmod(runtime_path, 0o600)
    finally:
        temporary_path.unlink(missing_ok=True)
    return runtime


def _restrict_runtime_directory(runtime_dir: Path) -> None:
    """让运行时目录及其子项只对当前操作系统用户开放。"""
    if os.name != 'nt':
        os.chmod(runtime_dir, 0o700)
        return

    identity_result = subprocess.run(
        ['whoami'],
        capture_output=True,
        check=True,
        encoding='utf-8',
        errors='replace',
        text=True,
    )
    identity = identity_result.stdout.strip()
    if not identity:
        raise RuntimeError('无法确定当前 Windows 用户，不能安全写入后端 token')
    acl_result = subprocess.run(
        [
            'icacls',
            str(runtime_dir),
            '/inheritance:r',
            '/grant:r',
            f'{identity}:(OI)(CI)F',
            '/T',
            '/C',
        ],
        capture_output=True,
        encoding='utf-8',
        errors='replace',
        text=True,
    )
    if acl_result.returncode != 0:
        detail = acl_result.stderr.strip() or acl_result.stdout.strip()
        raise RuntimeError(f'限制后端 token 文件权限失败：{detail}')


def read_backend_runtime(runtime_path: Path) -> BackendRuntime:
    """读取并严格校验主体运行时连接信息。"""
    payload: Any = json.loads(runtime_path.read_text('utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(f'{runtime_path} 顶层必须是 JSON 对象')

    port = payload.get('port')
    if not isinstance(port, int) or isinstance(port, bool) or port <= 0 or port > 65535:
        raise ValueError(f'{runtime_path} 缺少合法 port')
    token = payload.get('token')
    if not isinstance(token, str) or len(token) != 64:
        raise ValueError(f'{runtime_path} 缺少合法 token')
    try:
        bytes.fromhex(token)
    except ValueError as exc:
        raise ValueError(f'{runtime_path} 的 token 不是十六进制字符串') from exc
    return BackendRuntime(port=port, token=token)
