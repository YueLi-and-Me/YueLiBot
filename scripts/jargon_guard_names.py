"""黑话词表的人名兜底：撞名词条降级 pending，可疑词条出清单交人工。

两件事，刻意分开：

1. **自动降级**：词条与 ``identities.display_name``（可加 ``--names`` 补充
   bot 名、别名等）精确撞名的，整批降 ``pending``。词条是名字时释义必然
   讲的是那个人，「降级不删除」——遗忘不等于抹除，人工确认后可以再捞回来。
2. **只列不动**：释义里出现已知人名（``identities.display_name`` 与
   ``group_memberships.group_card`` 汇总）的词条，可能是「人物描述冒充
   黑话」（如「妹妹」），也可能是正好提到人的真黑话。这类判断错了会误伤
   真黑话，所以脚本只打清单，降级由人工过目后决定。

运行时侧另有兜底（agent/jargon.py 的 ``protected_names``）：bot 自己的名字
与别名在任何 status、任何路径下都不会被注入。本脚本是对存量数据的清洗，
两道防线互不替代。

用法示例：

    python scripts/jargon_guard_names.py data/memory.db
    python scripts/jargon_guard_names.py data/memory.db --names 月璃 小璃 璃宝 凌白
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Sequence, Set

from src.core.common.db.connection import open_db


def _load_identity_names(db: sqlite3.Connection) -> Set[str]:
    """汇总库里全部已知人名：平台显示名与各群名片。"""

    names: Set[str] = set()
    for row in db.execute('SELECT display_name FROM identities'):
        name = str(row['display_name']).strip()
        if name:
            names.add(name)
    for row in db.execute('SELECT group_card FROM group_memberships'):
        card = str(row['group_card']).strip()
        if card:
            names.add(card)
    return names


def _matching_terms(db: sqlite3.Connection, names: Sequence[str]) -> List[sqlite3.Row]:
    """取词条面与给定名字精确相等的词条（任意 status、任意作用域）。

    词表整表只有千级行，静态整表取回后内存比对，与 agent/jargon.py 的既有
    口径一致，也避免为 IN 列表动态拼接 SQL。
    """

    lowered = {name.strip().lower() for name in names}
    return [
        row for row in db.execute(
            'SELECT id, term, meaning, status, stream_id, hits FROM jargon')
        if str(row['term']).strip().lower() in lowered
    ]


def _suspect_terms(
    db: sqlite3.Connection,
    names: Sequence[str],
    min_name_len: int,
) -> List[tuple[sqlite3.Row, str]]:
    """取释义里出现已知人名的词条，附上命中的名字。

    名字太短（默认 <2 字）不参与：单字名会在释义里产生大量误报。
    """

    suspects: List[tuple[sqlite3.Row, str]] = []
    usable = [name for name in names if len(name) >= min_name_len]
    for row in db.execute('SELECT id, term, meaning, status, stream_id, hits FROM jargon'):
        meaning = str(row['meaning'])
        for name in usable:
            if name in meaning:
                suspects.append((row, name))
                break
    return suspects


def main() -> int:
    """执行撞名降级与可疑清单打印，返回进程退出码。"""

    parser = argparse.ArgumentParser(description='黑话词表人名兜底')
    parser.add_argument('database', type=Path, help='memory.db 路径')
    parser.add_argument(
        '--names', nargs='*', default=[],
        help='额外按词面降级的名字（bot 名、别名、对用户的称呼等）',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='只打印将要降级的词条，不写库',
    )
    args = parser.parse_args()

    db = open_db(args.database)
    identity_names = _load_identity_names(db)
    downgrade_names = identity_names | {n.strip() for n in args.names if n.strip()}

    collisions = _matching_terms(db, sorted(downgrade_names))
    print(f'已知人名（identities + 群名片）：{len(identity_names)} 个')
    print(f'与已知人名或指定名字撞名的词条：{len(collisions)} 条')
    for row in collisions:
        scope = '全局' if row['stream_id'] is None else f'stream {row["stream_id"]}'
        print(f'  降级 [{row["status"]} -> pending] 「{row["term"]}」'
              f'（{scope}，hits={row["hits"]}）')
    if collisions and not args.dry_run:
        with db:
            db.executemany(
                "UPDATE jargon SET status = 'pending' WHERE id = ?",
                [(row['id'],) for row in collisions],
            )
        print(f'已降级 {len(collisions)} 条（降级不删除，人工可再确认回来）')
    elif collisions:
        print('（dry-run，未写库）')

    suspects = _suspect_terms(db, sorted(identity_names), min_name_len=2)
    confirmed_suspects = [item for item in suspects if item[0]['status'] == 'confirmed']
    print(f'\n释义里出现已知人名的词条：{len(suspects)} 条'
          f'（其中 confirmed {len(confirmed_suspects)} 条）')
    print('以下清单仅供人工过目，脚本不做任何改动——判断错了会误伤真黑话：')
    for row, name in suspects:
        scope = '全局' if row['stream_id'] is None else f'stream {row["stream_id"]}'
        print(f'  [{row["status"]}] 「{row["term"]}」 释义提到「{name}」'
              f'（{scope}，hits={row["hits"]}）：{str(row["meaning"])[:60]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
