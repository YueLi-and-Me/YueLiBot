"""验证后端默认监听端口和端口占用错误的可操作性。

本模块属于后端启动入口测试，检查未指定端口时使用固定默认端口，并确认端口绑定失败时
抛出包含端口和 Windows 排查命令的 OSError。测试只调用启动解析和 socket 绑定逻辑，
不会启动完整后端服务。
"""
from __future__ import annotations

from typing import NoReturn

import argparse
import socket

import pytest

from src.main import _bind_backend_socket, main


class _ParserInspected(Exception):
    """参数解析器断言完成后立即终止 main，避免启动真实后端。"""


def test_backend_port_defaults_to_7999(monkeypatch: pytest.MonkeyPatch) -> None:
    """未显式传端口时，主体必须固定监听 7999。"""

    def inspect_parser(parser: argparse.ArgumentParser) -> NoReturn:
        port_action = next(
            action for action in parser._actions if '--port' in action.option_strings
        )
        assert port_action.default == 7999
        raise _ParserInspected

    monkeypatch.setattr(argparse.ArgumentParser, 'parse_args', inspect_parser)

    with pytest.raises(_ParserInspected):
        main()


def test_occupied_backend_port_has_actionable_error() -> None:
    """后端端口被占时给出中文原因和可执行的 Windows 排查命令。"""
    occupied = socket.socket()
    occupied.bind(('127.0.0.1', 0))
    port = occupied.getsockname()[1]
    try:
        with pytest.raises(OSError) as exc_info:
            _bind_backend_socket(port)
    finally:
        occupied.close()

    message = str(exc_info.value)
    assert str(port) in message
    assert '占用' in message
    assert 'Get-NetTCPConnection' in message
