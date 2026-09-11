"""启动期版本更新报告。

两条底线要有断言盯着：首次运行不得把出厂版本说成更新；更新日志缺失或状态文件
损坏时不得影响启动。另有一条机检性质的断言——当前 APP_VERSION 必须在更新日志里
有自己的小节，否则升级时控制台只会打一条警告，用户什么都看不到。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.app_meta import APP_VERSION
from src.core.runtime import update_notes

REPO_ROOT = Path(__file__).resolve().parents[2]

CHANGELOG = """\
# 更新日志

本文件记录每个发布版本的变更。

## [0.2.0] - 2026-10-01

### 新增

- 第一件事
- 第二件事

## [0.1.0] - 2026-09-01

- 出厂版本
"""


def test_解析出每个版本的小节(tmp_path: Path) -> None:
    """标题前的说明文字与标题下的小节标题都不该混进条目行。"""
    sections = update_notes.parse_changelog(CHANGELOG)

    assert set(sections) == {'0.2.0', '0.1.0'}
    date, lines = sections['0.2.0']
    assert date == '2026-10-01'
    # 标题下的空行属于 Markdown 排版，首行必须是内容而不是空行。
    assert lines[0] == '### 新增'
    assert lines[-2:] == ['- 第一件事', '- 第二件事']


def test_日期可省略() -> None:
    """标题不带日期时按空串处理，条目仍然要能取到。"""
    sections = update_notes.parse_changelog('## [0.3.0]\n\n- 没写日期\n')

    assert sections['0.3.0'] == ('', ['- 没写日期'])


def test_首尾空行被裁掉而中间保留() -> None:
    """条目行之间的空行是排版，不是内容，不能一起裁掉。"""
    _date, lines = update_notes.parse_changelog(
        '## [0.4.0] - 2026-11-01\n\n- 甲\n\n- 乙\n\n'
    )['0.4.0']

    assert lines == ['- 甲', '', '- 乙']


def test_标题前的说明不属于任何版本() -> None:
    """说明文字落在第一个标题之前，不该被算进第一个版本。"""
    sections = update_notes.parse_changelog('# 更新日志\n\n随便一段说明。\n\n## [0.1.0]\n- 条目\n')

    assert list(sections) == ['0.1.0']
    assert sections['0.1.0'][1] == ['- 条目']


def test_当前版本必须在更新日志里有自己的小节() -> None:
    """发版时最容易漏的一步：升了版本号却没补更新日志。

    漏掉的后果不是报错，而是升级后控制台只打一条 warning，用户看不到任何更新内容，
    所以这条断言必须跟着 APP_VERSION 一起走。
    """
    text = update_notes.changelog_path(REPO_ROOT).read_text(encoding='utf-8')

    assert APP_VERSION in update_notes.parse_changelog(text), (
        f'CHANGELOG.md 里缺少 {APP_VERSION} 的小节；升版本号时必须同时补上这一节'
    )


def test_更新日志缺条目时只警告不抛(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """更新日志没写这个版本时，仍然要记下版本号。

    否则每次启动都会重报一次同样的警告，而用户既看不懂也修不了。
    """
    project = tmp_path / 'repo'
    project.mkdir()
    (project / update_notes.CHANGELOG_FILENAME).write_text('# 空\n', encoding='utf-8')
    update_notes.write_last_version(tmp_path, '0.1.0')

    assert update_notes.announce_update(tmp_path, project, '0.2.0') is False
    assert update_notes.read_last_version(tmp_path) == '0.2.0'


def test_首次运行不报告更新(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """首次安装没有「升级前」，把出厂版本说成更新是错的。"""
    project = tmp_path / 'repo'
    project.mkdir()
    (project / update_notes.CHANGELOG_FILENAME).write_text(CHANGELOG, encoding='utf-8')
    printed: list[str] = []
    monkeypatch.setattr(update_notes, 'print_box', lambda *args, **kwargs: printed.append(args[0]))

    assert update_notes.announce_update(tmp_path, project, '0.2.0') is False
    assert printed == []
    assert update_notes.read_last_version(tmp_path) == '0.2.0'


def test_版本号未变不重复报告(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一版本第二次启动必须安静，否则每次开都会刷一屏更新说明。"""
    project = tmp_path / 'repo'
    project.mkdir()
    (project / update_notes.CHANGELOG_FILENAME).write_text(CHANGELOG, encoding='utf-8')
    update_notes.write_last_version(tmp_path, '0.1.0')
    printed: list[str] = []
    monkeypatch.setattr(update_notes, 'print_box', lambda *args, **kwargs: printed.append(args[0]))

    assert update_notes.announce_update(tmp_path, project, '0.1.0') is False
    assert printed == []


def test_升级时打印该版本的更新内容(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """控制台要看到版本跨度与全部条目，缺一条都算没告诉用户。"""
    project = tmp_path / 'repo'
    project.mkdir()
    (project / update_notes.CHANGELOG_FILENAME).write_text(CHANGELOG, encoding='utf-8')
    update_notes.write_last_version(tmp_path, '0.1.0')
    boxes: list[tuple] = []
    monkeypatch.setattr(
        update_notes,
        'print_box',
        lambda title, rows, **kwargs: boxes.append((title, list(rows))),
    )

    assert update_notes.announce_update(tmp_path, project, '0.2.0') is True
    assert len(boxes) == 1
    title, rows = boxes[0]
    assert title == '版本更新'
    assert rows[0] == '0.1.0 → 0.2.0（2026-10-01）'
    assert '- 第一件事' in rows and '- 第二件事' in rows
    assert update_notes.read_last_version(tmp_path) == '0.2.0'


def test_损坏的状态文件按首次运行处理(tmp_path: Path) -> None:
    """状态文件坏了只影响「是否报告」，不能把异常带进启动路径。"""
    update_notes.state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    update_notes.state_path(tmp_path).write_text('{ 这不是 JSON', encoding='utf-8')

    assert update_notes.read_last_version(tmp_path) is None


def test_状态文件字段缺失按首次运行处理(tmp_path: Path) -> None:
    """字段缺失与文件损坏同等对待，不额外报错。"""
    update_notes.state_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    update_notes.state_path(tmp_path).write_text('{"other": 1}', encoding='utf-8')

    assert update_notes.read_last_version(tmp_path) is None


def test_版本号往返读写(tmp_path: Path) -> None:
    """写进去的版本号要能原样读回来。"""
    update_notes.write_last_version(tmp_path, '1.2.3')

    assert update_notes.read_last_version(tmp_path) == '1.2.3'
