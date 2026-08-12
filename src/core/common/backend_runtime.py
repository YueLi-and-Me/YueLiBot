"""主体后端运行时连接信息的生成与读取。

本模块为本机 HTTP/WebSocket 服务生成端口和访问令牌，并将连接信息以受限权限写入数据
目录；Electron、命令行自检和适配器进程通过该文件共享后端连接坐标。
"""

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
    """生成后端连接坐标，并以受限权限原子写入运行时 JSON 文件。

    Args:
        data_dir: 应用运行时数据根目录；函数在其下创建 ``runtime`` 子目录。
        port: 后端 HTTP/WebSocket 监听端口，范围为 ``1`` 到 ``65535``。

    Returns:
        包含端口和 64 个十六进制字符认证 token 的 ``BackendRuntime``。

    Raises:
        ValueError: 端口不在合法范围内。
        OSError: 运行时目录、临时文件或目标文件无法创建、写入、同步或替换。
        RuntimeError: 当前操作系统用户无法确定，或 Windows 权限限制失败。

    Side Effects:
        生成新的随机 token，创建并限制 ``runtime`` 目录权限，原子更新
        ``backend.json``；临时文件在成功和异常路径都会清理。
    """
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
    """将运行时目录及其子项限制为当前操作系统用户可访问。

    Args:
        runtime_dir: 已存在的运行时目录路径。

    Raises:
        OSError: Unix 权限或 Windows ACL 操作失败。
        RuntimeError: 无法获取当前 Windows 用户，或 ACL 命令返回失败。

    Side Effects:
        Unix 修改目录权限为 ``0700``；Windows 关闭继承并为当前用户授予递归完全控制。
    """
    # Unix 使用目录权限；Windows 使用 ACL，避免把 token 保护逻辑混用到两套权限模型。
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
    # 关闭继承并只授予当前用户完全控制，防止父目录权限重新暴露 token 文件。
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
    """读取并严格校验后端运行时连接信息。

    Args:
        runtime_path: ``backend.json`` 文件路径。

    Returns:
        包含合法监听端口和 64 位十六进制 token 的 ``BackendRuntime``。

    Raises:
        OSError: 文件无法读取。
        json.JSONDecodeError: 文件内容不是合法 JSON。
        ValueError: 顶层不是对象、端口不在范围内、token 长度不是 64 或包含非十六进制字符。
    """
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
