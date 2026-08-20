"""统计群聊「回复指向」四项改动的实际效果，用于上线后的效果观察。

数据来源全部是 ``data/memory.db``：入站还原效果读 ``messages`` 正文形态，
出站引用与目标合规读 ``pipeline_events``（``kind=action_decision``）。脚本只读，
不写库、不联网。

四项指标与 ``docs/live-quality-fixes-4.md`` 的 R-1～R-4 一一对应：

- R-1 引用还原：正文里 ``[回复 某人：…]`` 与残留 ``[引用消息]`` 的比例；
- R-2 提及显示名：``@名字`` 与残留 ``@裸号`` 的比例；
- R-3 出站引用：按投递期判据重算每条 committed 回复本应挂引用与否，
  并指出目标是否缺平台编号（迁移前的旧消息挂不上）；
- R-4 目标合规：按 promptHash 分组的 committed / silent / illegal_action 分布，
  以及每条 illegal_action 的目标是否属于本回合批次人物（跨人物越界）。

用法：

    python scripts/reference_watch.py                 # 最近 6 小时
    python scripts/reference_watch.py --hours 24
    python scripts/reference_watch.py --hash d4102a35 # 只看指定提示词版本的决策
"""

from __future__ import annotations

from argparse import ArgumentParser
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import json
import re
import sqlite3
import sys


DEFAULT_DB = Path('data/memory.db')
# 裸 QQ 号的判据：`@` 紧跟 5 位以上纯数字。显示名解析成功后不会保留这种形态。
_BARE_MENTION = re.compile(r'@\d{5,}')


def _percent(part: int, total: int) -> str:
    """把占比渲染为「计数 (百分比)」文本，总数为 0 时避免除零。

    :param part: 分子计数。
    :param total: 分母计数；为 0 时只输出计数。
    :return: 形如 ``12 (34.3%)`` 的文本。
    """
    if total <= 0:
        return f'{part}'
    return f'{part} ({part / total * 100:.1f}%)'


def _inbound_stats(db: sqlite3.Connection, since: int) -> None:
    """打印入站引用还原与提及显示名的还原率。

    :param db: 已打开的只读数据库连接。
    :param since: 统计起点的毫秒时间戳。
    :return: 无返回值；结果直接打印到标准输出。
    """
    rows = db.execute(
        "SELECT content FROM messages WHERE role = 'user' AND created_at >= ?",
        (since,),
    ).fetchall()
    texts = [str(row['content'] or '') for row in rows]

    restored = sum(1 for text in texts if '[回复 ' in text)
    unresolved = sum(1 for text in texts if '[引用消息]' in text)
    quoted = restored + unresolved
    named = sum(1 for text in texts if '@' in text and not _BARE_MENTION.search(text))
    bare = sum(1 for text in texts if _BARE_MENTION.search(text))

    print(f'\n=== R-1 引用还原（{len(texts)} 条入站消息）===')
    print(f'  含引用：{quoted}')
    print(f'    已还原原文：{_percent(restored, quoted)}')
    print(f'    仍是占位符：{_percent(unresolved, quoted)}  ← 只应是撤回或超出协议端保留窗口')

    print(f'\n=== R-2 提及显示名（{len(texts)} 条入站消息）===')
    print(f'  含 @ 的消息：{named + bare}')
    print(f'    已解析成名字：{_percent(named, named + bare)}')
    print(f'    仍是裸号：{_percent(bare, named + bare)}  ← 应趋近 0')


def _decisions(db: sqlite3.Connection, since: int, prompt_hash: str) -> List[Dict[str, Any]]:
    """读取时间窗内的行动决策事件负载。

    :param db: 已打开的只读数据库连接。
    :param since: 统计起点的毫秒时间戳。
    :param prompt_hash: 只保留该 promptHash 的事件；空串表示不过滤。
    :return: 已解析的事件负载列表，按发生顺序排列；每项补入表列上的
        ``streamId``（该字段只在事件表的列上，不在 payload JSON 里）。
    """
    payloads: List[Dict[str, Any]] = []
    for row in db.execute(
        'SELECT stream_id, payload FROM pipeline_events '
        "WHERE kind = 'action_decision' AND at >= ? ORDER BY seq",
        (since,),
    ):
        payload = json.loads(row['payload'])
        version = payload.get('version') or {}
        if prompt_hash and version.get('promptHash') != prompt_hash:
            continue
        payload['streamId'] = row['stream_id']
        payloads.append(payload)
    return payloads


def _outbound_stats(db: sqlite3.Connection, payloads: List[Dict[str, Any]]) -> None:
    """按投递期判据重算每条 committed 回复本应挂引用与否。

    出站是否真的挂了引用不落库（适配器直接发协议 action），因此这里只能复算
    「应该挂」的条数，实际效果要到 QQ 里看气泡上有没有引用框。

    :param db: 已打开的只读数据库连接。
    :param payloads: 已按时间窗筛出的行动决策事件负载。
    :return: 无返回值；结果直接打印到标准输出。
    """
    committed = [item for item in payloads if item.get('eventStatus') == 'committed']
    should_quote = 0
    missing_id = 0
    samples: List[str] = []
    for item in committed:
        decision = item.get('decision') or {}
        targets = decision.get('targetMessageIds') or []
        stream_id = item.get('streamId')
        if not targets or stream_id is None:
            continue
        target_id = targets[0]
        displaced = db.execute(
            "SELECT 1 FROM messages WHERE stream_id = ? AND id > ? AND role = 'user' LIMIT 1",
            (stream_id, target_id),
        ).fetchone()
        if displaced is None:
            continue
        should_quote += 1
        row = db.execute(
            'SELECT external_message_id, content FROM messages WHERE id = ?',
            (target_id,),
        ).fetchone()
        external_id = row['external_message_id'] if row else None
        if external_id is None:
            missing_id += 1
        elif len(samples) < 5:
            text = ' '.join(str(row['content'] or '').split())[:28]
            reply = ' '.join(str((decision.get('reply') or {}).get('text', '')).split())[:28]
            samples.append(f'    引用 {external_id}「{text}」→ 回复「{reply}」')

    print(f'\n=== R-3 出站引用（{len(committed)} 条 committed 回复）===')
    print(f'  按判据应挂引用：{_percent(should_quote, len(committed))}')
    print(f'  其中目标缺平台编号、挂不上：{missing_id}  ← 迁移前落库的旧消息，会随时间归零')
    if samples:
        print('  最近几条应带引用的回复（去 QQ 里核对气泡上有没有引用框）：')
        for line in samples:
            print(line)


def _compliance_stats(db: sqlite3.Connection, payloads: List[Dict[str, Any]]) -> None:
    """打印决策状态分布，并逐条展开非法动作的越界形态。

    :param db: 已打开的只读数据库连接。
    :param payloads: 已按时间窗筛出的行动决策事件负载。
    :return: 无返回值；结果直接打印到标准输出。
    """
    status = Counter(item.get('eventStatus') for item in payloads)
    attempted = sum(
        count for name, count in status.items()
        if name not in ('gate_dropped',)
    )
    illegal = [item for item in payloads if item.get('eventStatus') in ('illegal_action', 'parse_error')]

    print(f'\n=== R-4 目标合规（{len(payloads)} 条决策事件）===')
    for name, count in status.most_common():
        print(f'  {name}: {count}')
    print(f'  真正调用模型的回合：{attempted}，其中失败 {_percent(len(illegal), attempted)}')
    for item in illegal:
        detail = str(item.get('detail') or '')
        batch_person = item.get('personId')
        cross = ''
        match = re.search(r'目标消息 (\d+) 不在', detail)
        if match:
            row = db.execute(
                'SELECT sender_person_id FROM messages WHERE id = ?',
                (int(match.group(1)),),
            ).fetchone()
            if row is not None:
                cross = (
                    '（跨人物）' if row['sender_person_id'] != batch_person
                    else '（同人物越界）'
                )
        print(f'    {item.get("eventStatus")}: {detail}{cross}')


def main() -> int:
    """解析参数并依次打印四项指标。

    :return: 进程退出码；数据库缺失时返回 1。
    """
    parser = ArgumentParser(description='观察群聊回复指向四项改动的效果')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='memory.db 路径')
    parser.add_argument('--hours', type=float, default=6.0, help='统计最近多少小时，默认 6')
    parser.add_argument('--hash', default='', help='只统计该 promptHash 的决策，留空表示全部')
    args = parser.parse_args()

    if not args.db.exists():
        print(f'找不到数据库：{args.db}', file=sys.stderr)
        return 1

    since = int(datetime.now().timestamp() * 1000) - int(args.hours * 3_600_000)
    db = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        window = datetime.fromtimestamp(since / 1000).strftime('%m-%d %H:%M')
        print(f'统计窗口：{window} 至今' + (f'，promptHash={args.hash}' if args.hash else ''))
        _inbound_stats(db, since)
        payloads = _decisions(db, since, args.hash)
        _outbound_stats(db, payloads)
        _compliance_stats(db, payloads)
    finally:
        db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
