"""启动期报告版本更新内容。

本模块只做一件事：把 :data:`~src.core.app_meta.APP_VERSION` 与上次运行时的版本号
比较，本机升级过就在控制台打印仓库根目录 ``CHANGELOG.md`` 里对应那一节。上次运行
的版本号落在数据目录的 ``runtime/update_notes.json``。

为什么不直接打印「本次提交列表」：

- 现象：更新内容要给用户看的是行为变化，而提交列表里混着实现步骤、重构与合并提交。
- 原因：两者的读者不同，粒度也不同，同一份文本服务不了两个读者。
- 后果：``CHANGELOG.md`` 是唯一事实源，控制台公告与发布流程的 Release 正文都读它，
  写两处必然走样。

首次运行不报告：那个安装没有「升级前」，把出厂版本说成更新是错的。

**任何失败都不得影响启动**：文件缺失、格式不符、状态文件损坏都只记 warning，返回
未报告。与 :mod:`src.core.runtime.telemetry` 同一取向——启动路径上的旁支功能不该
有能力拖垮主流程。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

import json
import re

from src.core.logging.console_layout import print_box
from src.core.logging.logger import get_logger

logger = get_logger(__name__)

# 仓库根目录下的更新日志文件名。发布流程（.github/workflows/release.yml）读同一个文件。
CHANGELOG_FILENAME = 'CHANGELOG.md'
# 上次运行时版本号的存放位置，落在数据目录的 runtime/ 下。
STATE_FILENAME = 'update_notes.json'
# 小节标题行：``## [0.1.3] - 2026-09-11``。版本号后面的日期可省略，其余部分不认。
_SECTION_HEADING = re.compile(r'^##\s+\[(?P<version>[^\]]+)\](?:\s*-\s*(?P<date>\S+))?\s*$')
# 控制台信息框的标题。
_ANNOUNCE_TITLE = '版本更新'


def changelog_path(project_root: Path) -> Path:
    """给出更新日志文件的位置。

    :param project_root: 仓库根目录。
    :return: ``<project_root>/CHANGELOG.md``；是否存在不在此校验。
    """
    return project_root / CHANGELOG_FILENAME


def state_path(data_dir: Path) -> Path:
    """给出上次运行版本号的落盘位置。

    :param data_dir: 运行时数据目录。
    :return: ``<data_dir>/runtime/update_notes.json``。
    """
    return data_dir / 'runtime' / STATE_FILENAME


def parse_changelog(text: str) -> Dict[str, Tuple[str, List[str]]]:
    """把更新日志正文解析成 ``{版本号: (日期, 条目行)}``。

    只认 :data:`_SECTION_HEADING` 形式的二级标题；标题前的文件说明与标题下的小节
    标题都丢弃。条目行保留原样（含缩进与 ``-`` 前缀），由展示层决定怎么呈现。

    :param text: ``CHANGELOG.md`` 的完整正文。
    :return: 版本号到 ``(日期, 条目行列表)`` 的映射；日期缺失时为空串。同名标题
        出现多次时后者覆盖前者。
    """
    sections: Dict[str, Tuple[str, List[str]]] = {}
    # None 表示当前不在任何版本小节内：文件开头的说明文字落在这一段并被丢弃。
    current: str | None = None
    date = ''
    body: List[str] = []
    for raw in text.splitlines():
        heading = _SECTION_HEADING.match(raw.strip())
        if heading is not None:
            if current is not None:
                sections[current] = (date, body)
            current = heading.group('version').strip()
            date = (heading.group('date') or '').strip()
            body = []
            continue
        if current is not None:
            body.append(raw.rstrip())
    if current is not None:
        sections[current] = (date, body)
    return {version: (day, _trim_blank(lines)) for version, (day, lines) in sections.items()}


def read_last_version(data_dir: Path) -> str | None:
    """读取上次运行时的版本号。

    :param data_dir: 运行时数据目录。
    :return: 版本号字符串；文件不存在、内容损坏或字段缺失时返回 ``None``。
        损坏按「首次运行」处理——重装或换数据目录都会得到这个结果，
        与它的实际含义一致。
    """
    value = read_state(data_dir).get('version')
    return value if value else None


def write_last_version(data_dir: Path, version: str) -> None:
    """记录本次运行的版本号。

    :param data_dir: 运行时数据目录；不存在时递归创建。
    :param version: 本次运行的版本号。
    :raises OSError: 目录或文件无法写入。
    """
    write_state(data_dir, {'version': version})


def read_state(data_dir: Path) -> Dict[str, str]:
    """读取状态文件里的全部字段。

    同一份文件要装「上次运行的版本」与「已公告给群里的版本」两类事实，各字段由不同
    功能各自写入。整体读改写而不是各写各的：两次分头写会让后写的那次覆盖前一次。

    :param data_dir: 运行时数据目录。
    :return: 字段名到字符串值的映射；文件不存在、内容损坏或值不是字符串时返回空字典。
        损坏当作「什么都不知道」——调用方据此走首次运行或重新公告，都不会更差。
    """
    path = state_path(data_dir)
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(document, dict):
        return {}
    return {
        key: value for key, value in document.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }


def write_state(data_dir: Path, fields: Dict[str, str]) -> None:
    """把给定字段合并进状态文件。

    :param data_dir: 运行时数据目录；不存在时递归创建。
    :param fields: 要写入的字段；同名字段覆盖原值，其余字段原样保留。
    :raises OSError: 目录或文件无法写入。
    """
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**read_state(data_dir), **fields}
    path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def announce_update(
    data_dir: Path,
    project_root: Path,
    version: str,
) -> bool:
    """版本升级时在控制台报告本次更新内容，并记下当前版本号。

    报告与记录是两件事，失败互不牵连：更新日志里查不到这个版本时仍然记下版本号，
    否则每次启动都会重报一次同样的警告。

    :param data_dir: 运行时数据目录，存放上次运行的版本号。
    :param project_root: 仓库根目录，更新日志在其下。
    :param version: 本次运行的版本号，通常传 :data:`~src.core.app_meta.APP_VERSION`。
    :return: 是否真的报告了更新内容。首次运行、版本号未变、更新日志缺失或该版本
        没有条目时为 ``False``。
    副作用：升级时向标准输出写一个信息框（并发布到 WebUI 日志面板），并写入状态文件。
    """
    previous = read_last_version(data_dir)
    if previous is None:
        # 首次运行：没有「升级前」，不报告。仍要记下版本号，下次启动才有比较基准。
        _remember(data_dir, version)
        return False
    if previous == version:
        return False

    notes = read_release_notes(project_root, version)
    if notes is None:
        logger.warning(
            'update_notes_missing',
            version=version,
            path=str(changelog_path(project_root)),
        )
    else:
        date, lines = notes
        print_box(
            _ANNOUNCE_TITLE,
            _box_rows(previous, version, date, lines),
            source=__name__,
        )
        logger.info('update_announced', previous=previous, version=version)
    _remember(data_dir, version)
    return notes is not None


def read_release_notes(
    project_root: Path,
    version: str,
) -> Tuple[str, List[str]] | None:
    """取出某个版本的更新条目。

    :param project_root: 仓库根目录。
    :param version: 目标版本号。
    :return: ``(日期, 条目行)``；文件不存在、无法解析或没有该版本时返回 ``None``。
    """
    path = changelog_path(project_root)
    if not path.is_file():
        return None
    try:
        sections = parse_changelog(path.read_text(encoding='utf-8'))
    except OSError as exc:
        logger.warning('update_notes_unreadable', path=str(path), error=str(exc))
        return None
    return sections.get(version)


def _remember(data_dir: Path, version: str) -> None:
    """记录本次运行的版本号，写不进去只记日志。

    :param data_dir: 运行时数据目录。
    :param version: 本次运行的版本号。
    """
    try:
        write_last_version(data_dir, version)
    except OSError as exc:
        logger.warning('update_state_unwritable', path=str(state_path(data_dir)), error=str(exc))


def _box_rows(
    previous: str,
    version: str,
    date: str,
    lines: List[str],
) -> List[str]:
    """拼出信息框的文本行。

    :param previous: 上次运行的版本号。
    :param version: 本次运行的版本号。
    :param date: 该版本的发布日期；为空时省略。
    :param lines: 更新日志里该小节的条目行。
    :return: 首行为版本跨度，其后为空行与条目行。
    """
    head = f'{previous} → {version}'
    if date:
        head += f'（{date}）'
    return [head, '', *lines]


def _trim_blank(lines: List[str]) -> List[str]:
    """去掉条目行列表首尾的空行，保留行内空行。

    小节标题下的空行是 Markdown 排版要求，不是内容：留着它只会让信息框多出一行空格。
    行内空行则要保留——条目之间靠它分组。

    :param lines: 原始条目行。
    :return: 首尾不含空行的列表；全为空行时返回空列表。
    """
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


__all__ = [
    'CHANGELOG_FILENAME',
    'STATE_FILENAME',
    'announce_update',
    'changelog_path',
    'parse_changelog',
    'read_last_version',
    'read_release_notes',
    'read_state',
    'state_path',
    'write_last_version',
    'write_state',
]
