"""一次性清理 data/emojis 目录中库里无记录的孤儿文件。

背景：启动校验 verify_integrity 只查「库→文件」一个方向，目录里有文件但
emoji 表里没有对应哈希的孤儿对它完全不可见，可以无限期堆下去。本脚本补
「文件→库」这一半：以 emoji 表的哈希列为白名单，目录里文件名不匹配任何
哈希的文件就是孤儿。

幂等设计：只删孤儿、不写数据库，跑第二遍时孤儿清单为空、输出 0 个。
删除是显式动作：默认执行删除，先跑 --dry-run 打印清单给用户过目；
--dry-run 不修改任何文件。

用法（仓库根目录）：
    python scripts/emoji_orphan_cleanup.py --dry-run
    python scripts/emoji_orphan_cleanup.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from src.core.services.emoji import EmojiLibrary

_MB = 1024 * 1024


def _open_read_only(path: Path) -> sqlite3.Connection:
    """以只读方式打开数据库，避免脚本写坏运行中的库。

    URI 的路径部分必须用 as_uri() 的三斜杠形式：Windows 盘符路径直接拼在
    ``file:`` 后面会被 SQLite 当成相对路径，静默打开另一个库，导致把
    已登记文件误判为孤儿。
    """

    uri_path = path.resolve().as_uri()[len('file:'):]
    return sqlite3.connect(f'file:{uri_path}?mode=ro', uri=True)


def main() -> int:
    """执行孤儿巡检，按参数决定只打印还是实际删除。

    :return: 0 表示正常结束；1 表示参数或环境错误。
    """

    parser = argparse.ArgumentParser(description='清理表情包目录里的孤儿文件')
    parser.add_argument(
        '--db',
        default='data/memory.db',
        help='SQLite 数据库路径（默认 data/memory.db）',
    )
    parser.add_argument(
        '--directory',
        default='data/emojis',
        help='表情包目录路径（默认 data/emojis）',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='只打印孤儿清单与统计，不删除任何文件',
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    directory = Path(args.directory).resolve()
    if not db_path.is_file():
        print(f'数据库不存在：{db_path}', file=sys.stderr)
        return 1
    if not directory.is_dir():
        print(f'表情包目录不存在：{directory}', file=sys.stderr)
        return 1

    db = _open_read_only(db_path)
    try:
        library = EmojiLibrary(db, directory)
        orphans = library.scan_orphans()
    finally:
        db.close()

    if not orphans:
        print(f'{directory} 中没有孤儿文件，目录与库完全对齐。')
        return 0

    total_bytes = sum(size for _path, size in orphans)
    print(f'孤儿文件清单（{len(orphans)} 个 / {total_bytes} 字节 / {total_bytes / _MB:.1f} MB）：')
    for path, size in orphans:
        print(f'  {path.name}  {size} 字节')
    if args.dry_run:
        print('--dry-run：以上文件不会删除。去掉 --dry-run 实际执行。')
        return 0

    removed = 0
    freed_bytes = 0
    for path, size in orphans:
        try:
            path.unlink()
        except OSError as exc:
            print(f'删除失败 {path.name}：{exc}', file=sys.stderr)
            continue
        removed += 1
        freed_bytes += size
    print(
        f'已删除孤儿文件 {removed} 个，释放 {freed_bytes} 字节 '
        f'（{freed_bytes / _MB:.1f} MB）；剩余孤儿 '
        f'{len(orphans) - removed} 个。'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
