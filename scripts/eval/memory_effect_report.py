"""量化记忆线在真机上的实际效果，输出可与历史基线对照的数字。

本脚本只读，不写库、不写日志、不调模型，可在 Bot 运行期间随时执行。

数据来源两处：

- ``data/memory.db``：库状态（schema 版本、事实规模、来源与槽位分布、向量覆盖）
  与 ``pipeline_events`` 事件账本（动作分布、记忆相关计数器）；
- ``data/logs/prompt/planner/*.json``：逐回合的规划器提示词转储，从中数出
  「本回合真正进了提示词的事实条数」——这是召回改造的成败判据，
  比「recall 动作被选中几次」更接近实际效果，因为被动召回不产生动作事件。

提示词转储按目录滚动保留固定份数，因此本脚本的观察窗口受限于该保留量；
需要更长跨度时应在窗口滚掉之前定期执行并留存输出。

用法::

    python scripts/eval/memory_effect_report.py
    python scripts/eval/memory_effect_report.py --split "2026-09-03 11:48"

``--split`` 给定时刻后，提示词统计会分成该时刻前后两段并列输出，
用于「改造前 vs 改造后」这类对照；不给则只统计全窗口。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import argparse
import glob
import json
import os
import sqlite3
import statistics
import sys

# 提示词转储里事实块的固定标记；与 agent/prompt.py 的分节标题一致。
FACT_BLOCK_TAG = '长期记忆'
EPISODE_BLOCK_TAG = '近期回想'
PROFILE_BLOCK_TAG = '你对他们的印象'

# 事件账本里与记忆线直接相关的计数器；缺失按 0 计。
MEMORY_EVENT_KINDS: Tuple[str, ...] = (
    'memory_fact',
    'memory_extract',
    'memory_spread',
    'memory_fact_scope_blocked',
    'memory_fact_conflict',
    'memory_fact_superseded',
    'memory_impression_failed',
    'memory_ppr_timeout',
    'knowledge_learned',
    'profile_refreshed',
)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """以只读模式打开库，避免与运行中的 Bot 争写锁。

    :param path: 库文件路径。
    :return: 只读连接。
    :raises sqlite3.OperationalError: 文件不存在或无法以只读方式打开。
    副作用：无。
    """

    return sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)


def _scalar(db: sqlite3.Connection, sql: str) -> object:
    """执行只返回单值的查询。

    :param db: 只读连接。
    :param sql: 单值查询语句。
    :return: 首行首列的值。
    :raises sqlite3.Error: 查询失败。
    副作用：无。
    """

    return db.execute(sql).fetchone()[0]


def _has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    """判断表是否已经具备某一列。

    库可能停在尚未迁移到位的版本上，统计前必须先判据，
    否则一条缺列的 SELECT 会让整份报告失败。

    :param db: 只读连接。
    :param table: 表名。
    :param column: 列名。
    :return: 列存在时为 ``True``；表不存在时同样为 ``False``。
    :raises sqlite3.Error: PRAGMA 查询失败。
    副作用：无。
    """

    rows = db.execute('SELECT name FROM pragma_table_info(?)', (table,)).fetchall()
    return column in {str(row[0]) for row in rows}


def report_store(db: sqlite3.Connection) -> None:
    """打印库侧状态：版本、事实规模、来源与槽位分布、向量覆盖。

    :param db: 只读连接。
    :return: 无返回值。
    :raises sqlite3.Error: 查询失败。
    副作用：向 stdout 打印。
    """

    print('== 库状态 ==')
    print(f'  user_version      {_scalar(db, "PRAGMA user_version")}')
    print(f'  quick_check       {_scalar(db, "PRAGMA quick_check")}')
    total = int(_scalar(db, 'SELECT COUNT(*) FROM facts'))
    print(f'  facts             {total}')
    print(f'  episodes          {_scalar(db, "SELECT COUNT(*) FROM episodes")}')
    print(f'  knowledge         {_scalar(db, "SELECT COUNT(*) FROM knowledge")}')

    if _has_column(db, 'facts', 'origin_kind'):
        rows = db.execute(
            'SELECT origin_kind, COUNT(*) FROM facts GROUP BY origin_kind ORDER BY 2 DESC'
        ).fetchall()
        detail = '、'.join(f'{name} {count}' for name, count in rows)
        print(f'  origin_kind       {detail}')
    else:
        print('  origin_kind       （列不存在，库尚未迁移到 v21）')

    if _has_column(db, 'facts', 'slot'):
        slotted = int(_scalar(db, "SELECT COUNT(*) FROM facts WHERE slot <> ''"))
        superseded = int(_scalar(db, 'SELECT COUNT(*) FROM facts WHERE superseded_by IS NOT NULL'))
        # 槽位为空的事实之间永不判冲突，因此「带槽位条数」是冲突检测的有效底数。
        print(f'  带槽位的事实      {slotted}（冲突检测的有效底数）')
        print(f'  已被取代的事实    {superseded}')
    else:
        print('  slot              （列不存在，库尚未迁移到 v22）')

    vec = int(_scalar(db, 'SELECT COUNT(*) FROM facts WHERE embedding IS NOT NULL'))
    print(f'  事实向量覆盖      {vec} / {total}')
    missing = int(_scalar(db, 'SELECT COUNT(*) FROM knowledge WHERE embedding IS NULL'))
    print(f'  知识缺向量        {missing}')


def report_events(db: sqlite3.Connection, split_ms: Optional[int]) -> None:
    """打印事件账本里的动作分布与记忆计数器。

    :param db: 只读连接。
    :param split_ms: 可选的分界毫秒时间戳；给定时按其前后分列。
    :return: 无返回值。
    :raises sqlite3.Error: 查询失败。
    副作用：向 stdout 打印。
    """

    print('\n== 事件账本 ==')
    span = db.execute('SELECT MIN(at), MAX(at) FROM pipeline_events').fetchone()
    if span[0] is None:
        print('  账本为空')
        return
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000).strftime('%m-%d %H:%M')
    print(f'  窗口              {fmt(span[0])} → {fmt(span[1])}')

    actions: Dict[str, List[int]] = {}
    for payload, at in db.execute(
        "SELECT payload, at FROM pipeline_events WHERE kind = 'action_decision'"
    ):
        try:
            decision = json.loads(payload).get('decision')
        except (TypeError, ValueError):
            continue
        if not isinstance(decision, dict):
            continue
        name = str(decision.get('action') or '')
        if not name:
            continue
        bucket = actions.setdefault(name, [0, 0])
        bucket[1 if split_ms is not None and at >= split_ms else 0] += 1

    label = '（前 / 后）' if split_ms is not None else ''
    print(f'  动作分布{label}')
    for name, (before, after) in sorted(actions.items(), key=lambda kv: -sum(kv[1])):
        value = f'{before} / {after}' if split_ms is not None else str(before)
        print(f'    {name:12} {value}')

    print(f'  记忆计数器{label}')
    for kind in MEMORY_EVENT_KINDS:
        before = int(_scalar(
            db, f"SELECT COUNT(*) FROM pipeline_events WHERE kind = '{kind}'"
        )) if split_ms is None else int(db.execute(
            'SELECT COUNT(*) FROM pipeline_events WHERE kind = ? AND at < ?', (kind, split_ms)
        ).fetchone()[0])
        if split_ms is None:
            print(f'    {kind:28} {before}')
            continue
        after = int(db.execute(
            'SELECT COUNT(*) FROM pipeline_events WHERE kind = ? AND at >= ?', (kind, split_ms)
        ).fetchone()[0])
        print(f'    {kind:28} {before} / {after}')


def _block_lines(request: dict, tag: str) -> Optional[List[str]]:
    """从一次请求的消息序列里取出指定分节的条目行。

    :param request: 转储里的 ``request`` 对象。
    :param tag: 分节标记，例如 ``长期记忆``。
    :return: 该分节下以 ``- `` 开头的条目行；分节不存在时返回 ``None``。
        返回空列表表示分节存在但没有条目，与「分节不存在」语义不同。
    副作用：无。
    """

    for message in request.get('messages', []):
        content = message.get('content', '')
        if isinstance(content, str) and content.startswith(f'[{tag}]'):
            return [line for line in content.splitlines() if line.startswith('- ')]
    return None


def _dump_timestamp(path: str) -> Optional[datetime]:
    """从转储文件名解析回合时刻。

    文件名形如 ``20260903_133242_298703_s3.json``，前 15 个字符是时间戳。

    :param path: 转储文件路径。
    :return: 解析出的时刻；文件名不合约定时返回 ``None``。
    副作用：无。
    """

    stem = os.path.basename(path)[:15]
    try:
        return datetime.strptime(stem, '%Y%m%d_%H%M%S')
    except ValueError:
        return None


def _summarize(name: str, rows: Sequence[Tuple[int, int, datetime]]) -> None:
    """打印一段回合样本的事实注入统计。

    :param name: 该段的名称。
    :param rows: ``(事实条数, 涉及人数, 时刻)`` 三元组序列。
    :return: 无返回值。
    副作用：向 stdout 打印。
    """

    if not rows:
        print(f'  {name}：无样本')
        return
    counts = [row[0] for row in rows]
    people = [row[1] for row in rows]
    zero = sum(1 for value in counts if value == 0)
    print(f'  {name}')
    print(f'    回合 {len(rows)}  跨度 {rows[0][2]:%m-%d %H:%M} → {rows[-1][2]:%m-%d %H:%M}')
    print(f'    事实条数  中位 {statistics.median(counts):.0f}  均值 {statistics.mean(counts):.1f}'
          f'  最大 {max(counts)}  为零的回合 {zero}/{len(counts)}')
    print(f'    涉及人数  中位 {statistics.median(people):.0f}  最大 {max(people)}')


def report_prompts(root: Path, split: Optional[datetime]) -> None:
    """统计逐回合提示词里实际注入的事实条数与涉及人数。

    条目行形如 ``- <人名> <正文>``，因此以首个空白前的词近似「这条事实关于谁」。
    该近似只用于观察召回是否覆盖多人，不参与任何判定。

    :param root: 规划器提示词转储目录。
    :param split: 可选分界时刻；给定时分成前后两段并列输出。
    :return: 无返回值。
    副作用：向 stdout 打印。
    """

    print('\n== 提示词实际注入 ==')
    files = sorted(glob.glob(str(root / '*.json')))
    if not files:
        print(f'  {root} 下没有转储')
        return

    before: List[Tuple[int, int, datetime]] = []
    after: List[Tuple[int, int, datetime]] = []
    for path in files:
        moment = _dump_timestamp(path)
        if moment is None:
            continue
        try:
            with open(path, encoding='utf-8') as handle:
                dump = json.load(handle)
        except (OSError, ValueError):
            continue
        lines = _block_lines(dump.get('request', {}), FACT_BLOCK_TAG)
        if lines is None:
            continue
        owners = {line[2:].split()[0] for line in lines if len(line) > 3}
        row = (len(lines), len(owners), moment)
        (after if split is not None and moment >= split else before).append(row)

    if split is None:
        _summarize('全窗口', before)
        return
    _summarize(f'{split:%m-%d %H:%M} 之前', before)
    _summarize(f'{split:%m-%d %H:%M} 之后', after)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """解析参数并输出完整报告。

    :param argv: 可选参数序列；省略时读取 ``sys.argv``。
    :return: 进程退出码；库不存在时为 ``1``。
    副作用：向 stdout 打印报告。
    """

    parser = argparse.ArgumentParser(description='量化记忆线在真机上的实际效果')
    parser.add_argument('--data-dir', default='data', help='运行时数据目录，默认 data')
    parser.add_argument('--split', default='', help='对照分界时刻，形如 "2026-09-03 11:48"')
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    db_path = data_dir / 'memory.db'
    if not db_path.is_file():
        print(f'找不到库文件：{db_path}', file=sys.stderr)
        return 1

    split: Optional[datetime] = None
    if args.split:
        split = datetime.strptime(args.split, '%Y-%m-%d %H:%M')

    db = _connect_readonly(db_path)
    try:
        report_store(db)
        report_events(db, int(split.timestamp() * 1000) if split else None)
    finally:
        db.close()
    report_prompts(data_dir / 'logs' / 'prompt' / 'planner', split)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
