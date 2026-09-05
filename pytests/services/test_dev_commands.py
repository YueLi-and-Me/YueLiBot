"""开发者命令 /git、/version 的验收（任务书 B-2～B-5）。

B-2 是本包重点：天数解析层把一切非法输入拒绝在 fork 之前；合法天数只能
以「固定前缀 + 日期常量」的参数数组形态到达 git，不经 shell。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
import os
import platform
import re
import sqlite3
import subprocess

import pytest

from src.core.app_meta import APP_VERSION
from src.core.config.schema import CONFIG_VERSION
from src.core.services.dev import dev_commands

# B-2：逐类非法天数。前八类是格式错误，其后是越界，最后是 shell 元字符注入尝试。
REJECTED_DAY_INPUTS = [
    'abc',
    '',
    '   ',
    '3.5',
    'seven',
    '７',  # 全角数字
    '7_0',
    '+7',
    '-1',
    '-0',
    '0',
    '00',
    '3651',
    '99999',
    '7; rm -rf /',
    '7 && echo pwned',
    '7 | cat /etc/passwd',
    '$(rm -rf /)',
    '`rm -rf /`',
    '7\ndays',
]

# B-3：三个提交分别落在 1 小时、30 小时、240 小时（约十天）前。
COMMIT_HOURS_AGO = (1.0, 30.0, 240.0)


def _expected_reply(days: int, count: int) -> str:
    """按与被测模块同源的窗口算法拼出期望回复。"""
    start, end = dev_commands._window_dates(days, date.today())
    return (
        f'{dev_commands._display_date(start)}-{dev_commands._display_date(end)}'
        f'累计提交 {count} 次'
    )


def _expected_count(days: int) -> int:
    """按窗口下界数出已知时刻的提交里落在窗口内的个数。"""
    start = datetime.combine(date.today() - timedelta(days=days), time.min)
    now = datetime.now()
    return sum(
        1 for hours in COMMIT_HOURS_AGO if now - timedelta(hours=hours) >= start
    )


# ---------------------------------------------------------------------------
# B-2 天数解析与注入防护
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('text', REJECTED_DAY_INPUTS)
def test_b2_invalid_day_rejected_before_any_fork(
    monkeypatch: pytest.MonkeyPatch, text: str,
) -> None:
    """每个非法输入都得到拒绝文案，且处理过程不 fork 任何子进程。"""

    def _no_fork(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        raise AssertionError(f'输入 {text!r} 应在解析层拒绝，不允许到达子进程')

    monkeypatch.setattr(dev_commands.subprocess, 'run', _no_fork)
    reply = dev_commands.handle_git(text)
    assert '天数' in reply
    assert (
        f'{dev_commands.MIN_GIT_DAYS}～{dev_commands.MAX_GIT_DAYS}' in reply
    )


@pytest.mark.parametrize(
    'text, expected',
    [('1', 1), ('3650', 3650), (' 7 ', 7), ('007', 7), (None, 7)],
)
def test_b2_parse_days_accepts_bounds_and_defaults(text: str | None, expected: int) -> None:
    assert dev_commands.parse_days(text) == expected


def test_b2_valid_day_reaches_git_as_fixed_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """合法天数以参数数组传给 git：不经 shell，用户文本不出现在 argv。"""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(
        argv: list[str], **kwargs: object,
    ) -> subprocess.CompletedProcess:
        calls.append((argv, kwargs))
        if 'rev-parse' in argv:
            return subprocess.CompletedProcess(argv, 0, stdout='true\n', stderr='')
        return subprocess.CompletedProcess(argv, 0, stdout='12\n', stderr='')

    monkeypatch.setattr(dev_commands.subprocess, 'run', fake_run)
    reply = dev_commands.handle_git('7', tmp_path)

    expected_since = f'--since={(date.today() - timedelta(days=7)):%Y-%m-%d} 00:00:00'
    assert reply == _expected_reply(7, 12)
    assert len(calls) == 2
    count_argv, count_kwargs = calls[1]
    assert count_argv[0] == 'git'
    assert count_argv[1:4] == ['rev-list', '--count', expected_since]
    assert count_argv[4] == 'HEAD'
    assert count_kwargs.get('shell') in (None, False)
    assert count_kwargs.get('cwd') == tmp_path
    for part in count_argv:
        assert ';' not in part and '$' not in part and '`' not in part


def test_window_dates_span_days_back_to_today() -> None:
    """/git 30 在 2026.9.4 的窗口是 2026.8.5～2026.9.4，回复写这个区间。"""
    start, end = dev_commands._window_dates(30, date(2026, 9, 4))
    assert (start, end) == (date(2026, 8, 5), date(2026, 9, 4))
    assert dev_commands._display_date(start) == '2026.8.5'


# ---------------------------------------------------------------------------
# B-3 已知提交数的临时仓库
# ---------------------------------------------------------------------------

def _run_git(repo: Path, *args: str, hours_ago: float | None = None) -> None:
    """在临时仓库里执行一条 git 命令；指定 hours_ago 时固定作者与提交时刻。"""
    env = dict(os.environ)
    if hours_ago is not None:
        stamp = (
            datetime.now() - timedelta(hours=hours_ago)
        ).strftime('%Y-%m-%dT%H:%M:%S')
        env['GIT_AUTHOR_DATE'] = stamp
        env['GIT_COMMITTER_DATE'] = stamp
    subprocess.run(
        ['git', *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo_with_three_commits(tmp_path: Path) -> Path:
    """一个含 3 个已知时刻空提交的临时仓库。

    提交按从旧到新的顺序打入：git 的 ``--since`` 遍历在遇到早于下界的提交
    时会停止，HEAD 必须是新日期，否则计数恒为 0，不构成有效样本。
    """
    repo = tmp_path / 'repo'
    repo.mkdir()
    _run_git(repo, 'init', '-q')
    _run_git(repo, 'config', 'user.email', 'test@example.com')
    _run_git(repo, 'config', 'user.name', '测试')
    for hours in sorted(COMMIT_HOURS_AGO, reverse=True):
        _run_git(repo, 'commit', '--allow-empty', '-m', f'c{hours}', hours_ago=hours)
    return repo


def test_b3_counts_and_wording(repo_with_three_commits: Path) -> None:
    repo = repo_with_three_commits
    # 一、二天窗口的下界在「昨天零点」附近，30 小时前的提交是否计入取决于
    # 当前时刻，期望值按窗口推导；十天前的提交对七天窗口不可见、对三十天
    # 窗口可见，这两个断言与执行时刻无关。
    assert dev_commands.handle_git('1', repo) == _expected_reply(1, _expected_count(1))
    assert dev_commands.handle_git('2', repo) == _expected_reply(2, _expected_count(2))
    assert dev_commands.handle_git(None, repo) == _expected_reply(7, 2)
    assert dev_commands.handle_git('30', repo) == _expected_reply(30, 3)
    # 省略天数按默认 7 天窗口统计，与显式 7 完全同词。
    assert dev_commands.handle_git(None, repo) == dev_commands.handle_git('7', repo)


def test_b3_reply_carries_explicit_date_range(repo_with_three_commits: Path) -> None:
    """回复里是点分起止日期加累计次数，窗口自身可核对。"""
    reply = dev_commands.handle_git('30', repo_with_three_commits)
    assert re.fullmatch(
        r'\d{4}\.\d{1,2}\.\d{1,2}-\d{4}\.\d{1,2}\.\d{1,2}累计提交 \d+ 次', reply,
    )


def test_b3_matches_git_log_reference(repo_with_three_commits: Path) -> None:
    """计数与同一起始日的 git log --since 完全一致。"""
    repo = repo_with_three_commits
    start, _ = dev_commands._window_dates(7, date.today())
    reference = subprocess.run(
        ['git', 'log', f'--since={start:%Y-%m-%d} 00:00:00', '--oneline'],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().splitlines()
    assert dev_commands.handle_git('7', repo) == _expected_reply(7, len(reference))


# ---------------------------------------------------------------------------
# B-4 非 git 目录
# ---------------------------------------------------------------------------

def test_b4_plain_directory_reports_not_a_repository(tmp_path: Path) -> None:
    plain = tmp_path / 'plain'
    plain.mkdir()
    reply = dev_commands.handle_git('7', plain)
    assert '不是 git 仓库' in reply


def test_b4_broken_git_pointer_reports_not_a_repository(tmp_path: Path) -> None:
    broken = tmp_path / 'broken'
    broken.mkdir()
    (broken / '.git').write_text('gitdir: ../nowhere', encoding='utf-8')
    reply = dev_commands.handle_git('7', broken)
    assert '不是 git 仓库' in reply


# ---------------------------------------------------------------------------
# B-5 /version 字段溯源
# ---------------------------------------------------------------------------

def test_b5_fields_trace_to_real_sources(tmp_path: Path) -> None:
    db_path = tmp_path / 'memory.db'
    db = sqlite3.connect(db_path)
    db.execute('PRAGMA user_version = 27')
    db.close()
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    (config_dir / 'adapter.toml').write_text(
        "plugin = 'yueli-test-adapter'\n", encoding='utf-8',
    )
    app_dir = tmp_path / 'app'
    (app_dir / 'node_modules' / 'electron').mkdir(parents=True)
    (app_dir / 'node_modules' / 'electron' / 'package.json').write_text(
        '{"version": "43.2.1"}', encoding='utf-8',
    )

    reply = dev_commands.handle_version(db_path, config_dir, app_dir)

    assert APP_VERSION in reply
    assert CONFIG_VERSION in reply
    assert 'v27' in reply
    assert platform.python_version() in reply
    assert '43.2.1' in reply
    assert 'yueli-test-adapter' in reply


def test_b5_electron_falls_back_to_declared_range_and_marks_unavailable(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / 'app'
    app_dir.mkdir()
    (app_dir / 'package.json').write_text(
        '{"devDependencies": {"electron": "^43.2.0"}}', encoding='utf-8',
    )

    reply = dev_commands.handle_version(tmp_path / 'missing.db', tmp_path, app_dir)

    assert '^43.2.0' in reply
    assert reply.count('不可用') == 2  # 数据库与适配器声明都缺失


def test_register_dev_commands_wires_into_channel(tmp_path: Path) -> None:
    """合流接缝：两条命令进得了通道的注册表，正则能捕获天数。

    原来这里测的是本模块自带的 handle_help——一份硬编码清单。合流时删掉了它：
    通道自带的 /help 会枚举注册表当场生成清单，两份并存必然随命令增减而漂移。
    改测真正的接缝——它是合流时手工接的，出错的地方在这里而不在处理函数里。
    """
    from src.core.commands import registered_commands

    # 注册表是进程内全局状态，重复注册会抛；同一次 pytest 里可能已被别的用例注册过。
    if '/git' not in {item.name for item in registered_commands()}:
        dev_commands.register_dev_commands(tmp_path / 'memory.db', tmp_path)

    catalog = {item.name: item for item in registered_commands()}
    assert '/git' in catalog and '/version' in catalog
    assert '默认 7' in catalog['/git'].description
    assert catalog['/git'].owner_required and catalog['/version'].owner_required

    matched = re.fullmatch(catalog['/git'].pattern, '/git 30')
    assert matched is not None and matched.group('arg') == '30'
    assert re.fullmatch(catalog['/git'].pattern, '/git') is not None
    assert re.fullmatch(catalog['/version'].pattern, '/version') is not None
