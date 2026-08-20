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


def runtime_file_path(data_dir: Path) -> Path:
    """返回运行时凭据文件在数据目录中的位置。

    :param data_dir: 应用运行时数据根目录。

    :return: ``<data_dir>/runtime/backend.json``。

    调用方需要展示或读取这个位置时一律走本函数，不要再各自拼一次路径：
    TS 侧（`supervisor.ts`）和适配器入口已经各持有一份字面量，Python 内部再拼第三份，
    改目录结构时必然漏改其中一处，而漏改的表现是「文件明明在那儿却读不到」。
    """

    return data_dir / 'runtime' / 'backend.json'


def create_backend_runtime(data_dir: Path, port: int) -> BackendRuntime:
    """生成后端连接坐标，并以受限权限原子写入运行时 JSON 文件。

    :param data_dir: 应用运行时数据根目录；函数在其下创建 ``runtime`` 子目录。
    :param port: 后端 HTTP/WebSocket 监听端口，范围为 ``1`` 到 ``65535``。

    :return: 包含端口和 64 个十六进制字符认证 token 的 ``BackendRuntime``。

    :raises ValueError: 端口不在合法范围内。
    :raises OSError: 运行时目录、临时文件或目标文件无法创建、写入、同步或替换。
    :raises RuntimeError: 当前操作系统用户无法确定，或 Windows 权限限制失败。

    副作用：
        生成新的随机 token，创建并限制 ``runtime`` 目录权限，原子更新
        ``backend.json``；临时文件在成功和异常路径都会清理。
    """
    if port <= 0 or port > 65535:
        raise ValueError('主体 backend 端口必须在 1 到 65535 之间')

    runtime = BackendRuntime(port=port, token=secrets.token_hex(32))
    runtime_dir = data_dir / 'runtime'
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _restrict_runtime_directory(runtime_dir)

    runtime_path = runtime_file_path(data_dir)
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


def _current_user_sid() -> str:
    """返回当前进程访问令牌所属用户的 SID 文本。

    :return: 形如 ``S-1-5-21-<domain>-<rid>`` 的 SID 字符串。

    :raises OSError: 打开进程令牌、查询 TokenUser 或转换 SID 文本的 Win32 调用失败。

    不经 ``whoami`` 子进程取用户名：该命令在 PATH 上可能命中 MSYS/Git Bash 附带的
    同名实现，返回不带域前缀的裸用户名。
    - 现象：``icacls`` 把裸名解析成同名计算机账户的 SID（只有域部分、没有 RID），
      与真实用户 SID 相差末尾的 RID。
    - 原因：``/inheritance:r`` 先砍掉全部继承 ACE，随后完全控制被授予这个不存在的
      主体，目录对包括当前用户在内的所有非管理员账户零权限。
    - 后果：目录事后无法枚举、无法删除，连读取 DACL 都返回 Access denied，只能靠
      提权 takeown 抢回所有权。测试用例每跑一次就会遗留一个这样的临时目录。
    直接读进程令牌可彻底绕开名称解析这一层。
    """
    # ctypes.wintypes 在非 Windows 平台导入即失败，且 WinDLL 只在 Windows 存在；
    # 本函数仅由 _restrict_runtime_directory 的 nt 分支调用，故在函数内导入。
    from ctypes import wintypes

    import ctypes

    TOKEN_QUERY = 0x0008
    TOKEN_USER_CLASS = 1  # TOKEN_INFORMATION_CLASS::TokenUser

    advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    # 必须显式声明签名：ctypes 默认按 c_int 处理返回值，64 位下会截断句柄与指针，
    # 表现为随机的无效句柄错误而非直接崩溃。
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        # 首次调用只为问出 TOKEN_USER 所需字节数，必然以 ERROR_INSUFFICIENT_BUFFER
        # 失败，因此这里不检查返回值。
        advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, TOKEN_USER_CLASS, buffer, size, ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER 的首个成员是 SID_AND_ATTRIBUTES::Sid，即缓冲区开头的一个指针。
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return sid_text.value
        finally:
            # ConvertSidToStringSidW 的输出缓冲区由 LocalAlloc 分配，须由调用方释放。
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def _restrict_runtime_directory(runtime_dir: Path) -> None:
    """将运行时目录及其子项限制为当前操作系统用户可访问。

    :param runtime_dir: 已存在的运行时目录路径。

    :raises OSError: Unix 权限操作失败，或 Windows 查询当前用户 SID 的 Win32 调用失败。
    :raises RuntimeError: ACL 命令返回失败。

    副作用：
        Unix 修改目录权限为 ``0700``；Windows 关闭继承并为当前用户授予递归完全控制。
    """
    # Unix 使用目录权限；Windows 使用 ACL，避免把 token 保护逻辑混用到两套权限模型。
    if os.name != 'nt':
        os.chmod(runtime_dir, 0o700)
        return

    identity = _current_user_sid()
    # 关闭继承并只授予当前用户完全控制，防止父目录权限重新暴露 token 文件。
    # 主体以 * 前缀的 SID 给出，绕开 icacls 的名称解析：裸用户名会被解析成同名的
    # 计算机账户，导致目录对当前用户也不可访问，详见 _current_user_sid 的说明。
    acl_result = subprocess.run(
        [
            'icacls',
            str(runtime_dir),
            '/inheritance:r',
            '/grant:r',
            f'*{identity}:(OI)(CI)F',
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

    :param runtime_path: ``backend.json`` 文件路径。

    :return: 包含合法监听端口和 64 位十六进制 token 的 ``BackendRuntime``。

    :raises OSError: 文件无法读取。
    :raises json.JSONDecodeError: 文件内容不是合法 JSON。
    :raises ValueError: 顶层不是对象、端口不在范围内、token 长度不是 64 或包含非十六进制字符。
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
