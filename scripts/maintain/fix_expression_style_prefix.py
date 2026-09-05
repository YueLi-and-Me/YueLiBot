"""E-1：剥掉 expressions.style 开头的「使用」二字。

历史学习提示词的示例以「使用」开头（如「使用 我嘞个xxxx」），模型把前缀一并
学了回来，真机库里 42% 的 style 是「使用反问句加强语气」这类指令句式。注入
模板是「可以用"{style}"来表达」，不剥会拼成「可以用"使用反问句加强语气"来表达」。

脚本只改 style 文本，不动 use_count 等任何历史值，也不删行。剥前缀后与既有行
撞唯一键 ``(situation, style, stream_id)`` 的，保持原样并在报告里列出——真机
数据实测为零冲突，分支仅为兜底。幂等：已剥的行不再匹配 ``使用%``，重跑零改动。

收尾会顺手检查学习示例原文（我嘞个xxxx / 对对对 / 这么强！）有没有泄漏进
style，命中只报告不处理，是否清理由人决定。

用法示例：

    python scripts/maintain/fix_expression_style_prefix.py data/memory.db --dry-run
    python scripts/maintain/fix_expression_style_prefix.py data/memory.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

_PREFIX = '使用'
# 学习提示词里的三条示例原文；`xxxx` 是占位符，正常内容不可能带着它。
_EXAMPLE_PROBES = ('我嘞个xxxx', '对对对', '这么强！')
_COMMIT_ROWS = 200


@dataclass
class FixReport:
    """一次前缀修复的计数报告。

    :ivar matched: 本次扫描到以「使用」开头的行数。
    :ivar updated: 实际剥掉前缀并落库的行数。
    :ivar skipped_empty: 剥后为空、保持原样的行数。
    :ivar skipped_conflict: 剥后撞唯一键、保持原样的行数。
    :ivar example_leaks: 剥后仍含学习示例原文的行（id, style），只报告不处理。
    """

    matched: int = 0
    updated: int = 0
    skipped_empty: int = 0
    skipped_conflict: int = 0
    example_leaks: list[tuple[int, str]] = field(default_factory=list)

    @property
    def changed(self) -> int:
        """返回本轮真正写库的行数，供幂等性对账。"""
        return self.updated


def _existing_keys(db: sqlite3.Connection) -> set[tuple[str, str, object]]:
    """读取 expressions 全部唯一内容键，供剥前缀后的冲突预判。"""

    return {
        (str(row[0]), str(row[1]), row[2])
        for row in db.execute('SELECT situation, style, stream_id FROM expressions')
    }


def fix(db: sqlite3.Connection, dry_run: bool = False) -> FixReport:
    """剥掉 style 开头的「使用」并回写，返回计数报告。

    :param db: 已打开的 SQLite 连接，需含 expressions 表。
    :param dry_run: 为真时只扫描与计数，不写库。
    :return: 本次修复的计数与示例泄漏清单。
    :raises sqlite3.Error: 读写 expressions 失败时抛出。
    副作用：
        非 dry_run 时每 200 行提交一次；只 UPDATE style 列，不触碰其他列、
        不删除任何行。
    """

    report = FixReport()
    rows = db.execute(
        "SELECT id, situation, style, stream_id FROM expressions WHERE style LIKE '使用%'"
    ).fetchall()
    report.matched = len(rows)
    keys = _existing_keys(db)
    pending = 0
    for row in rows:
        row_id, situation, style, stream_id = int(row[0]), str(row[1]), str(row[2]), row[3]
        new_style = style[len(_PREFIX):].strip()
        if not new_style:
            report.skipped_empty += 1
            continue
        # 剥完与既有行（含已剥好的自己之外任意行）撞键时保持原样，不合并也不删除。
        if (situation, new_style, stream_id) in keys:
            report.skipped_conflict += 1
            continue
        if not dry_run:
            db.execute('UPDATE expressions SET style = ? WHERE id = ?', (new_style, row_id))
            pending += 1
        keys.add((situation, new_style, stream_id))
        report.updated += 1
        if not dry_run and pending >= _COMMIT_ROWS:
            db.commit()
            pending = 0
    if not dry_run:
        db.commit()

    for probe in _EXAMPLE_PROBES:
        for row in db.execute(
            'SELECT id, style FROM expressions WHERE style LIKE ?',
            (f'%{probe}%',),
        ):
            report.example_leaks.append((int(row[0]), str(row[1])))
    return report


def main(argv: list[str] | None = None) -> int:
    """命令行入口：对指定数据库执行一次前缀修复并打印报告。"""

    parser = argparse.ArgumentParser(description='剥掉 expressions.style 开头的「使用」二字')
    parser.add_argument('db', type=Path, help='目标 SQLite 数据库路径，如 data/memory.db')
    parser.add_argument('--dry-run', action='store_true', help='只扫描与计数，不写库')
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(f'数据库不存在：{args.db}', file=sys.stderr)
        return 1

    db = sqlite3.connect(str(args.db))
    try:
        report = fix(db, dry_run=args.dry_run)
    finally:
        db.close()
    mode = '（dry-run，未写库）' if args.dry_run else ''
    print(f'扫描到「使用」开头 {report.matched} 行{mode}：')
    print(f'  已剥前缀：{report.updated}')
    if report.skipped_empty:
        print(f'  剥后为空、保持原样：{report.skipped_empty}')
    if report.skipped_conflict:
        print(f'  剥后撞唯一键、保持原样：{report.skipped_conflict}')
    if report.example_leaks:
        print(f'仍含学习示例原文 {len(report.example_leaks)} 行（仅报告，未处理）：')
        for row_id, style in report.example_leaks:
            print(f'  id={row_id}  style={style!r}')
    else:
        print('未发现学习示例原文泄漏。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
