"""分区列的静态不变式。

存在的理由：`messages.stream_id` / `episodes.stream_id` / `facts.person_id` 在 DDL 里
都是 `NOT NULL DEFAULT 1`。这个默认值删不掉——SQLite 没有 ALTER COLUMN，而 `episodes`
同时被 `messages.episode_id` 和 `episode_cues.episode_id` 引用，重建它正是迁移方案里
明令禁止的动作。

于是留下一个静默陷阱：**任何漏写分区列的 INSERT 都会安静地落进 1 号分区**，
也就是桌面会话 / owner，不报错。今天够不着（MemoryStore 的方法签名强制传参），
但接了 QQ 之后，新写的 SQL 一旦漏写，群聊消息就会混进桌面历史。

类型系统无法检查 SQL 字符串，因此本模块使用静态扫描验证分区列约束。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Tuple

import re

SRC = Path(__file__).resolve().parents[2] / 'src'

# 表名 → 这张表的 INSERT 必须显式点名的分区列
REQUIRED_COLUMNS = {
    'messages': 'stream_id',
    'episodes': 'stream_id',
    'facts': 'person_id',
}

# INSERT INTO <表> ( ...列表... )   —— 列清单可能跨行
_INSERT = re.compile(
    r'INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\s*\(([^)]*)\)',
    re.IGNORECASE | re.DOTALL,
)


def _inserts() -> Iterator[Tuple[Path, str, str]]:
    """扫出 src/ 下所有带列清单的 INSERT。"""
    for path in SRC.rglob('*.py'):
        source = path.read_text(encoding='utf-8')
        for match in _INSERT.finditer(source):
            yield path, match.group(1).lower(), match.group(2)


def test_every_insert_names_its_partition_column() -> None:
    offenders = []
    for path, table, columns in _inserts():
        required = REQUIRED_COLUMNS.get(table)
        if required is None:
            continue
        named = {c.strip().lower() for c in columns.split(',')}
        if required not in named:
            offenders.append(f'{path.relative_to(SRC.parent)} 的 INSERT INTO {table} 漏写 {required}')

    assert not offenders, (
        '这些 INSERT 会靠 DDL 的 DEFAULT 1 静默落进桌面/owner 分区：\n  '
        + '\n  '.join(offenders)
    )


def test_the_scan_actually_finds_the_known_inserts() -> None:
    """守卫上面那条扫描本身。

    正则一旦失效，上一条会因为「一个 INSERT 都没扫到」而假绿——
    那是这类静态检查最典型的坏法。
    """
    tables = [table for _, table, _ in _inserts()]

    for table in REQUIRED_COLUMNS:
        assert table in tables, f'扫描没找到任何 INSERT INTO {table}，正则可能已失效'
