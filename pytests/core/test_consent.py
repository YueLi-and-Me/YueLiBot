"""首次启动的用户协议同意闸门。

这道闸拦在配置生成与建库之前，任何一处出错的后果都是「所有人都起不来」，
因此三条路径全部要有断言：未同意时拦住、接受后放行、无人可询问时给出可自解
的指引而不是笼统失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import json

import pytest

from src.core.runtime import consent


def test_没有记录时视为未同意(tmp_path: Path) -> None:
    """全新数据目录里没有记录文件，必须按未同意处理。"""
    assert consent.has_consented(tmp_path) is False


def test_记录之后不再询问(tmp_path: Path) -> None:
    """写入记录后判定为已同意，且文件落在数据目录下。"""
    path = consent.record_consent(tmp_path, channel='console')

    assert path == consent.consent_path(tmp_path)
    assert consent.has_consented(tmp_path) is True


def test_记录里不含任何身份信息(tmp_path: Path) -> None:
    """这份文件只用来「别再问第二遍」，不是凭证，不得记录身份。"""
    consent.record_consent(tmp_path, channel='console')

    document = json.loads(consent.consent_path(tmp_path).read_text(encoding='utf-8'))

    assert set(document) == {'version', 'acceptedAt', 'channel'}


def test_协议版本提高后重新询问(tmp_path: Path) -> None:
    """旧版本的同意记录不能替新版本背书。"""
    consent.consent_path(tmp_path).write_text(
        json.dumps({'version': consent.AGREEMENT_VERSION - 1, 'acceptedAt': 1}),
        encoding='utf-8',
    )

    assert consent.has_consented(tmp_path) is False


def test_损坏的记录按未同意处理(tmp_path: Path) -> None:
    """解析失败时重问一次，而不是把用户卡在看不懂的报错上。"""
    consent.consent_path(tmp_path).write_text('{ 这不是 JSON', encoding='utf-8')

    assert consent.has_consented(tmp_path) is False
    assert consent.read_consent(tmp_path) is None


def test_命令行接受后放行并落记录(tmp_path: Path) -> None:
    """--accept-agreement 是无头场景唯一的接受方式，必须真的写记录。"""
    consent.require_consent(tmp_path, Path('.'), preaccepted=True)

    assert consent.has_consented(tmp_path) is True
    document = json.loads(consent.consent_path(tmp_path).read_text(encoding='utf-8'))
    assert document['channel'] == 'flag'


def test_无人可询问时退出且不留记录(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非交互环境不得询问，也不得默认放行。

    现象：作为 systemd 服务启动时 stdin 不是终端，直接 input() 会立刻 EOF，
    进程在重启循环里反复起停。这里断言它改为打印指引并以非零码退出，
    且不写同意记录——没人同意过就不能留下同意的痕迹。
    """
    class _NotATerminal:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr('sys.stdin', _NotATerminal())

    with pytest.raises(SystemExit) as excinfo:
        consent.require_consent(tmp_path, Path('.'))

    assert excinfo.value.code != 0
    assert consent.consent_path(tmp_path).exists() is False


def test_终端说谎时也不会卡住(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """isatty() 报真但 input() 立刻 EOF 的情况同样要走无头指引。

    被 supervisor 或容器接管时会分配伪终端却无人输入，只信 isatty 判据不够。
    """
    class _LyingTerminal:
        @staticmethod
        def isatty() -> bool:
            return True

    def _eof(_prompt: str = '') -> str:
        raise EOFError

    monkeypatch.setattr('sys.stdin', _LyingTerminal())
    monkeypatch.setattr('builtins.input', _eof)

    with pytest.raises(SystemExit) as excinfo:
        consent.require_consent(tmp_path, Path('.'))

    assert excinfo.value.code != 0
    assert consent.consent_path(tmp_path).exists() is False


def test_输入不是同意就退出(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """必须逐字输入同意词；y、yes、回车都不算。"""
    class _Terminal:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr('sys.stdin', _Terminal())
    monkeypatch.setattr('builtins.input', lambda _prompt='': 'y')

    with pytest.raises(SystemExit) as excinfo:
        consent.require_consent(tmp_path, Path('.'))

    assert excinfo.value.code != 0
    assert consent.consent_path(tmp_path).exists() is False


def test_逐字输入同意词后放行(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入同意词后写记录并返回，不再抛出。"""
    class _Terminal:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr('sys.stdin', _Terminal())
    monkeypatch.setattr('builtins.input', lambda _prompt='': f'  {consent.CONSENT_WORD}  ')

    consent.require_consent(tmp_path, Path('.'))

    assert consent.has_consented(tmp_path) is True
    document = json.loads(consent.consent_path(tmp_path).read_text(encoding='utf-8'))
    assert document['channel'] == 'console'


def test_协议正文随代码分发并声明由AI生成() -> None:
    """协议必须在仓库里，且必须自陈由 AI 生成、提示读者甄别。

    这两句是用户明确要求的，改写协议时最容易被「润色」掉——「AI 辅助生成」
    这类措辞暗示有人执笔而 AI 只是帮忙，与事实不符。断言盯住的是这层含义，
    不是具体排版。
    """
    agreement = Path(consent.AGREEMENT_FILENAME)

    assert agreement.is_file()
    text = agreement.read_text(encoding='utf-8')
    assert 'AI 生成' in text, '协议必须声明由 AI 生成'
    assert '甄别' in text, '协议必须提示读者甄别'
