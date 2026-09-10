"""后端端口绑定的地址复用与端口占用的排查提示。

后端被反复重启时，旧连接会以 TIME_WAIT 的形式继续占用本地地址；没有 SO_REUSEADDR
时 `bind()` 会因此失败，systemd 随即落进 auto-restart 循环，而 `ss -lptn` 此时输出为空，
照报错提示去查只会更困惑。本文件锁两件事：

- 非 win32 平台在 `bind()` 之前打开地址复用，win32 平台保持原有语义——那里的
  SO_REUSEADDR 允许另一个进程真正抢占已绑定的地址，与「该 socket 需持续持有」冲突；
- 端口绑不上时的提示要点出 TIME_WAIT 这条岔路，并给出能看见非 LISTEN 状态的命令。

依赖 ``src.main``。
"""

from __future__ import annotations

from typing import Any

import socket
import sys

import pytest

from src import main
from src.main import _bind_backend_socket

# Linux 的 EADDRINUSE。不能写 errno.EADDRINUSE：Windows 上该常量取的是 100，
# 与 src.main 里识别的那一组值（98、10048）对不上，用例会悄悄走错分支。
LINUX_EADDRINUSE = 98


class _RefusingSocketModule:
    """让 `bind()` 以指定 errno 失败的 socket 模块替身。

    真实占用端口在 Windows 上给不出 Linux 的 errno：地址复用一旦打开，抢占语义会让
    失败码变成 10013。替身只负责让 Linux 分支的提示文案可以在任意平台上验证。
    """

    SOL_SOCKET = socket.SOL_SOCKET
    SO_REUSEADDR = socket.SO_REUSEADDR

    def __init__(self, errno_value: int) -> None:
        self._errno_value = errno_value
        self.closed = False

    def socket(self) -> '_RefusingSocketModule':
        """返回自身，使临时 socket 的创建与使用落在同一个替身上。"""

        return self

    def setsockopt(self, *args: Any) -> None:
        """地址复用开关本身不是本用例的观测点。"""

    def bind(self, address: tuple[str, int]) -> None:
        """按构造时给定的 errno 拒绝绑定。"""

        raise OSError(self._errno_value, 'Address already in use')

    def close(self) -> None:
        """记录关闭动作，供用例确认失败路径没有漏关 socket。"""

        self.closed = True


def test_backend_socket_enables_address_reuse_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 win32 平台绑定后必须带着 SO_REUSEADDR，否则重启会撞上 TIME_WAIT。"""

    monkeypatch.setattr(sys, 'platform', 'linux')
    sock = _bind_backend_socket(0)
    try:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 1
    finally:
        sock.close()


def test_backend_socket_keeps_windows_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """win32 平台不得设置 SO_REUSEADDR：那里的语义是允许其他进程抢占该地址。"""

    monkeypatch.setattr(sys, 'platform', 'win32')
    sock = _bind_backend_socket(0)
    try:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
    finally:
        sock.close()


def test_occupied_port_hint_points_at_states_beyond_listen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端口绑不上的提示必须指出 TIME_WAIT，并给出列出全部连接状态的命令。"""

    monkeypatch.setattr(sys, 'platform', 'win32')
    occupied = socket.socket()
    occupied.bind(('127.0.0.1', 0))
    port = occupied.getsockname()[1]
    try:
        with pytest.raises(OSError) as exc_info:
            _bind_backend_socket(port)
    finally:
        occupied.close()

    message = str(exc_info.value)
    assert 'TIME_WAIT' in message
    assert f'Get-NetTCPConnection -LocalPort {port}' in message
    assert f'netstat -ano | findstr ":{port}"' in message


def test_linux_hint_asks_for_a_listing_without_the_listen_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux 提示必须给「只看 LISTEN」与「列出全部状态」两条命令。"""

    stub = _RefusingSocketModule(LINUX_EADDRINUSE)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(main, 'socket', stub)

    with pytest.raises(OSError) as exc_info:
        _bind_backend_socket(7999)

    message = str(exc_info.value)
    assert 'TIME_WAIT' in message
    assert 'ss -lptn "sport = :7999"' in message
    assert 'ss -ant "sport = :7999"' in message
    assert stub.closed, '绑定失败后必须关掉临时 socket'
