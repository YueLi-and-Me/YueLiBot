"""实现开发者命令 /git、/version、/help 的只读处理函数。

本模块只负责命令的解析与执行，不负责消息匹配、owner 鉴权与入站拦截——
那些属于命令通道。对接接缝：命令通道落地后，为下列三条命令各注册一个条目，
正则命中后把参数原文交给对应处理函数，把返回的字符串原样回复给 owner：

    /git     建议 ``^/git(?:\\s+(?P<arg>.+))?$``  → ``handle_git(arg)``
    /version 建议 ``^/version\\s*$``              → ``handle_version(db_path, config_dir)``
    /help    建议 ``^/help\\s*$``                 → ``handle_help()``

所有命令只读：不改仓库状态、不写数据库、不出网。/git 是全项目唯一 fork
外部进程的位置，其安全约束见 :func:`count_recent_commits`，任何改动都不得
放宽「用户文本不进入子进程」这条边界。
"""

from __future__ import annotations

from pathlib import Path
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple
import json
import platform
import re
import sqlite3
import subprocess
import tomllib

from src.core.app_meta import APP_VERSION
from src.core.common.logger import get_logger
from src.core.common.self_check import open_readonly_database
from src.core.config.adapter_selection import read_active_adapter
from src.core.config.schema import CONFIG_VERSION

logger = get_logger(__name__)

# 本模块文件位置是 src/core/services/dev_commands.py，上推三级即仓库根。
PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_GIT_DAYS = 7
MIN_GIT_DAYS = 1
# 上界取十年（3650 天）：本项目的历史远短于十年，更大的窗口没有诊断意义，
# 超出上界的输入几乎必然是多敲了零；解析层据此把这类输入直接拒绝。
MAX_GIT_DAYS = 3650

# 两条 git 只读命令的正常耗时在亚秒级；超时按命令失败处理，不重试。
GIT_TIMEOUT_SECONDS = 5.0

# 天数只接受 1～4 位 ASCII 数字。re.ASCII 排除全角数字；正负号、小数、
# 下划线分隔（int('7_0') == 70）与非数字字符都被 fullmatch 挡下。
_DAY_TEXT_PATTERN = re.compile(r'\d{1,4}', re.ASCII)


class NotGitRepositoryError(Exception):
    """repo_dir 不在 git 工作树内时由 :func:`count_recent_commits` 抛出。"""


class GitCommandError(Exception):
    """git 无法启动、超时或以非零退出码结束时抛出。"""


def parse_days(raw: str | None) -> int:
    """把 /git 的参数文本解析为窗口天数。

    :param raw: 命令参数原文；省略时返回 :data:`DEFAULT_GIT_DAYS`。
    :return: 闭区间 ``[MIN_GIT_DAYS, MAX_GIT_DAYS]`` 内的整数天数。
    :raises ValueError: 参数不是纯 ASCII 数字串，或超出上下界。

    这是用户输入进入子进程前的唯一关卡：命令通道传来的任何文本都止步于
    本函数，只有通过校验的整数才允许参与构造 git 参数。
    """
    if raw is None:
        return DEFAULT_GIT_DAYS
    text = raw.strip()
    if not _DAY_TEXT_PATTERN.fullmatch(text):
        raise ValueError(f'天数必须是 {MIN_GIT_DAYS}～{MAX_GIT_DAYS} 的整数，例如 /git 7')
    days = int(text)
    if days < MIN_GIT_DAYS or days > MAX_GIT_DAYS:
        raise ValueError(f'天数要在 {MIN_GIT_DAYS}～{MAX_GIT_DAYS} 之间')
    return days


def handle_git(argument: str | None, repo_dir: Path = PROJECT_ROOT) -> str:
    """处理 /git 命令，返回给人看的单行回复。

    :param argument: 命令参数原文；``None`` 表示省略天数，按默认窗口统计。
    :param repo_dir: 统计目标仓库内的任意目录；默认为本项目所在签出。
    :return: 「起止日期累计提交 N 次」形态的回复；非法输入返回拒绝文案，
        不向调用方抛异常。

    统计窗口是「N 天前的零点」到「现在」，回复里的起止日期与传给 git 的
    查询窗口同源，数字可以直接按日期复核。
    """
    try:
        days = parse_days(argument)
    except ValueError as exc:
        return str(exc)
    start, end = _window_dates(days, date.today())
    try:
        count = count_recent_commits(start, repo_dir)
    except NotGitRepositoryError:
        # 发行版没有 .git，这是预期分支而不是缺陷；不做任何回退统计。
        return '这里不是 git 仓库，/git 只能在 git 签出里统计提交。'
    except GitCommandError:
        return 'git 命令没有执行成功，详情见主体日志。'
    return f'{_display_date(start)}-{_display_date(end)}累计提交 {count} 次'


def _window_dates(days: int, today: date) -> Tuple[date, date]:
    """把天数换算成统计窗口的起止日期：``[today - days, today]``。

    :param days: 已通过 :func:`parse_days` 校验的天数。
    :param today: 计算基准日，测试可注入。
    :return: (起始日, 结束日)；起始日零点即 git 查询的下界。
    """
    return today - timedelta(days=days), today


def _display_date(value: date) -> str:
    """回复用的日期写法：点分、月与日不补零（2026.9.4）。"""
    return f'{value.year}.{value.month}.{value.day}'


def handle_version(db_path: Path, config_dir: Path, repo_dir: Path = PROJECT_ROOT) -> str:
    """处理 /version 命令，把运行时版本要素合并为一条消息。

    :param db_path: 主体 SQLite 数据库路径，读取其 ``user_version``。
    :param config_dir: 主体配置目录，读取当前适配器声明。
    :param repo_dir: 仓库根，用于定位 package.json 与 node_modules。
    :return: 两行中文文本；个别字段读取失败时以「不可用」占位，不抛异常。

    每个字段都来自运行时读取的真实来源：应用版本来自 :data:`APP_VERSION`，
    配置格式来自 schema 的 :data:`CONFIG_VERSION`，其余字段读取对应文件或
    数据库，不写死任何字面量。
    """
    first_line = (
        f'月璃版本 {APP_VERSION}；配置格式 {CONFIG_VERSION}；'
        f'数据库 {_read_user_version_text(db_path)}'
    )
    second_line = (
        f'Python {platform.python_version()}；'
        f'Electron {_read_electron_version_text(repo_dir)}；'
        f'适配器 {_read_adapter_text(config_dir)}'
    )
    return f'{first_line}\n{second_line}'


def handle_help() -> str:
    """处理 /help 命令，返回开发者命令清单。"""
    return '\n'.join(_HELP_LINES)


_HELP_LINES: Tuple[str, ...] = (
    '可用命令：',
    '/git [天数]：统计最近 N 天的提交次数并给出起止日期，默认 7，上限 3650，'
    '只在 git 签出里可用',
    '/version：查看应用版本、配置格式、数据库版本、Python/Electron 版本和当前适配器',
    '/help：显示本清单',
)


def count_recent_commits(window_start: date, repo_dir: Path) -> int:
    """统计 repo_dir 所在 git 仓库自 window_start 零点以来的提交次数。

    :param window_start: 窗口起始日；以本地零点为下界，与回复展示的日期
        区间同一口径。
    :param repo_dir: 仓库内的任意目录；git 以它为工作目录。
    :return: 当前 HEAD 分支上的提交次数，与
        ``git log --since='<window_start> 00:00:00' --oneline | wc -l``
        同一口径。
    :raises NotGitRepositoryError: repo_dir 不在 git 工作树内。
    :raises GitCommandError: git 无法启动、超时、非零退出（含仓库还没有
        任何提交导致 HEAD 不存在的情形）。

    安全约束（全项目唯一的子进程调用点，不得放宽）：git 以参数数组启动，
    不经 shell；argv 由固定前缀加日期常量构成，日期来自已校验整数的天数
    换算，用户文本不进入 argv。两条命令都只读，不触碰任何远端。
    """
    _require_work_tree(repo_dir)
    since = f'--since={window_start:%Y-%m-%d} 00:00:00'
    argv: List[str] = ['git', 'rev-list', '--count', since, 'HEAD']
    stdout = _run_git(argv, repo_dir)
    return int(stdout.strip())


def _require_work_tree(repo_dir: Path) -> None:
    """确认 repo_dir 在 git 工作树内，否则抛出 :class:`NotGitRepositoryError`。"""
    try:
        stdout = _run_git(['git', 'rev-parse', '--is-inside-work-tree'], repo_dir)
    except GitCommandError as exc:
        raise NotGitRepositoryError(str(exc)) from exc
    if stdout.strip() != 'true':
        raise NotGitRepositoryError(f'{repo_dir} 不在 git 工作树内')


def _run_git(argv: List[str], repo_dir: Path) -> str:
    """执行一条只读 git 命令并返回标准输出。

    :param argv: 以 ``git`` 开头的参数数组。
    :param repo_dir: 子进程工作目录。
    :return: 标准输出全文。
    :raises GitCommandError: git 无法启动、超时或非零退出；三种情况都先记日志。
    副作用：fork 一个 git 子进程；不写入任何文件，不触碰网络与远端。
    """
    try:
        result = subprocess.run(
            argv,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            errors='replace',
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        logger.error('dev_git_unavailable', argv=argv[1:], reason=str(exc))
        raise GitCommandError('git 无法启动') from exc
    except subprocess.TimeoutExpired as exc:
        logger.error('dev_git_timeout', argv=argv[1:], timeoutSeconds=GIT_TIMEOUT_SECONDS)
        raise GitCommandError(f'git 在 {GIT_TIMEOUT_SECONDS:g} 秒内未返回') from exc
    if result.returncode != 0:
        logger.error(
            'dev_git_command_failed',
            argv=argv[1:],
            exitCode=result.returncode,
            stderr=_clip(result.stderr),
        )
        raise GitCommandError(f'git 退出码 {result.returncode}')
    return result.stdout


def _clip(text: str, limit: int = 200) -> str:
    """把日志里的 stderr 截到给定长度，避免长报错刷屏。"""
    text = (text or '').strip()
    return text if len(text) <= limit else text[:limit] + '…'


def _read_user_version_text(db_path: Path) -> str:
    """只读读取数据库 ``user_version``，失败时记日志并返回「不可用」。"""
    try:
        db = open_readonly_database(db_path)
    except (OSError, sqlite3.Error) as exc:
        logger.error('dev_version_db_unreadable', path=str(db_path), reason=str(exc))
        return '不可用'
    try:
        row = db.execute('PRAGMA user_version').fetchone()
        version = int(row[0])
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.error('dev_version_db_unreadable', path=str(db_path), reason=str(exc))
        return '不可用'
    finally:
        db.close()
    return f'v{version}'


def _read_electron_version_text(repo_dir: Path) -> str:
    """读取 Electron 版本：先取已安装包的精确版本，取不到再退回声明区间。

    两个来源都是仓库内的真实文件：``node_modules/electron/package.json``
    的 ``version`` 是实际安装的版本；不可得时读根 ``package.json`` 的
    ``devDependencies.electron``（形如 ``^43.2.0`` 的声明区间）。
    """
    try:
        installed = _read_json_object(
            repo_dir / 'node_modules' / 'electron' / 'package.json'
        )
        version = installed.get('version')
        if isinstance(version, str) and version:
            return version
    except (OSError, ValueError):
        pass
    try:
        declared = _read_json_object(repo_dir / 'package.json')
        range_text = declared.get('devDependencies', {}).get('electron')
        if isinstance(range_text, str) and range_text:
            return range_text
    except (OSError, ValueError) as exc:
        logger.error('dev_version_electron_unreadable', repoDir=str(repo_dir), reason=str(exc))
    return '不可用'


def _read_adapter_text(config_dir: Path) -> str:
    """读取当前适配器目录名，失败时记日志并返回「不可用」。"""
    try:
        return read_active_adapter(config_dir)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        logger.error('dev_version_adapter_unreadable', configDir=str(config_dir), reason=str(exc))
        return '不可用'


def _read_json_object(path: Path) -> Dict[str, Any]:
    """读取一个必须是 JSON 对象的文件。

    :raises OSError: 文件不存在或无法读取。
    :raises ValueError: 内容不是合法 JSON，或顶层不是对象。
    """
    with open(path, 'rb') as file:
        document = json.load(file)
    if not isinstance(document, dict):
        raise ValueError(f'{path} 顶层不是 JSON 对象')
    return document
