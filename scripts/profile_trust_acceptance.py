"""人物画像信任分级 · 真机副本验收脚本。

在真机库的临时副本上走完整链路并打印验收数字：

1. 备份式复制真机库（源库全程只读），迁移到当前版本；
2. 迁移后立刻统计确凿档为空的画像数（存量不重算，应全部为空）；
3. 全量置脏后跑两轮刷新：第一轮真实调用 ``memory`` 模型槽，
   第二轮证据未变、应全部走证据指纹短路；
4. 对全部确凿档条目做 id 级对账（每条必须对应一条真实有效的 fact），
   并随机抽 3 份逐条打印供人工核对；
5. 打印同一个人迁移前画像正文与刷新后两档正文的对照。

用法（在仓库根目录）：

    uv run python scripts/profile_trust_acceptance.py \
        --db data/memory.db --config config

临时目录的路径会打印出来，里面的副本与事件账本可留作复查。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sqlite3
import sys
import tempfile
from pathlib import Path

from src.core.agent.profile import (
    mark_dirty,
    parse_confirmed,
    profiles_for_injection,
    refresh_profiles,
    render_evidence,
)
from src.core.common.db.connection import open_db
from src.core.common.db.migrations.manager import get_user_version, run_migrations
from src.core.config.loader import load_config
from src.core.llm_models.router import create_routers
from src.core.observe.store import event_store


class _CountingProvider:
    """包住 memory 槽路由，统计实际进入模型的流式调用次数。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    def stream(self, *args, **kwargs):
        self.calls += 1
        return self._inner.stream(*args, **kwargs)


def _copy_database(source: Path, workdir: Path) -> Path:
    """用 SQLite 备份 API 复制库（源只读，WAL 里未落盘的页也会带上）。"""

    dest = workdir / source.name
    src = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def _audit_confirmed(db: sqlite3.Connection) -> tuple[int, int, list[str]]:
    """对账全部确凿档条目：fact_id 必须存在、正文一致、仍有效。"""

    violations: list[str] = []
    persons_with_confirmed = 0
    total_entries = 0
    rows = db.execute(
        "SELECT person_id, confirmed FROM person_profile WHERE confirmed <> ''"
    ).fetchall()
    for row in rows:
        person_id = int(row['person_id'])
        entries = parse_confirmed(str(row['confirmed']))
        if entries:
            persons_with_confirmed += 1
        total_entries += len(entries)
        for entry in entries:
            fact = db.execute(
                'SELECT content, active, superseded_by FROM facts WHERE id = ?',
                (entry.fact_id,),
            ).fetchone()
            if fact is None:
                violations.append(f'person {person_id}: fact {entry.fact_id} 不存在')
            elif str(fact['content']) != entry.content:
                violations.append(f'person {person_id}: fact {entry.fact_id} 正文不一致')
            elif not int(fact['active']) or fact['superseded_by'] is not None:
                violations.append(f'person {person_id}: fact {entry.fact_id} 已失效')
    return persons_with_confirmed, total_entries, violations


async def _run(args: argparse.Namespace) -> int:
    workdir = Path(tempfile.mkdtemp(prefix='yueli-profile-trust-'))
    print(f'工作目录（副本与事件账本在此，可复查）：{workdir}')
    copy = _copy_database(Path(args.db), workdir)

    cfg = load_config(Path(args.config))
    routers = create_routers(cfg)
    if not routers.memory.ready:
        print('memory 模型槽不可用，无法跑真实刷新', file=sys.stderr)
        return 1
    provider = _CountingProvider(routers.memory)
    generation = cfg.generation.memory

    event_store.configure(workdir / 'events.db')
    db = open_db(copy)

    # ---- 迁移前快照（供最后的新旧对照） -------------------------------------
    pre_version = get_user_version(db)
    pre_rows = db.execute(
        'SELECT person_id, summary FROM person_profile ORDER BY person_id'
    ).fetchall()
    total = len(pre_rows)
    pre_summary = {int(row['person_id']): str(row['summary']) for row in pre_rows}
    pre_nonempty = sum(1 for text in pre_summary.values() if text.strip())
    print(f'\n== 迁移前 == user_version={pre_version}  画像总数={total}  summary 非空={pre_nonempty}')

    # ---- 迁移 ----------------------------------------------------------------
    run_migrations(db, copy)
    post_version = get_user_version(db)
    empty_confirmed = db.execute(
        "SELECT COUNT(*) FROM person_profile WHERE confirmed = ''"
    ).fetchone()[0]
    print(f'== 迁移后 == user_version={post_version}  确凿档为空：{empty_confirmed}/{total}')
    kept = sum(
        1
        for row in db.execute('SELECT person_id, summary FROM person_profile').fetchall()
        if pre_summary.get(int(row['person_id'])) == str(row['summary'])
    )
    print(f'存量 summary 原样保留：{kept}/{total}')

    # ---- 第一轮刷新：指纹全空，凡有证据必调模型 -------------------------------
    person_ids = sorted(pre_summary)
    evidence_empty = sum(
        1 for pid in person_ids if render_evidence(db, pid).is_empty()
    )
    mark_dirty(db, person_ids)
    before_calls = provider.calls
    processed1 = await refresh_profiles(
        db, provider,
        bot_name=cfg.bot.name,
        temperature=generation.temperature,
        max_tokens=generation.token_limit,
        limit=len(person_ids) + 10,
    )
    calls1 = provider.calls - before_calls
    skips1 = len(event_store.search(kinds=['profile_refresh_skipped']).events)
    still_dirty1 = db.execute(
        'SELECT COUNT(*) FROM person_profile WHERE dirty = 1'
    ).fetchone()[0]
    print(
        f'== 第一轮刷新 == 参与={len(person_ids)}  处理={processed1}  '
        f'调用模型={calls1}  指纹短路={skips1}  无证据免调={evidence_empty}  '
        f'失败留脏={still_dirty1}'
    )
    persons_with_confirmed, total_entries, violations = _audit_confirmed(db)
    print(f'确凿档非空画像：{persons_with_confirmed}  确凿条目总数：{total_entries}  id 对账违例：{len(violations)}')
    for item in violations[:10]:
        print(f'  ! {item}')

    # ---- 第二轮刷新：证据未变，应全部短路 -------------------------------------
    mark_dirty(db, person_ids)
    event_store.clear()
    before_calls = provider.calls
    processed2 = await refresh_profiles(
        db, provider,
        bot_name=cfg.bot.name,
        temperature=generation.temperature,
        max_tokens=generation.token_limit,
        limit=len(person_ids) + 10,
    )
    calls2 = provider.calls - before_calls
    skips2 = len(event_store.search(kinds=['profile_refresh_skipped']).events)
    print(
        f'== 第二轮刷新 == 参与={len(person_ids)}  处理={processed2}  '
        f'调用模型={calls2}  指纹短路={skips2}  '
        f'（短路+调用={skips2 + calls2}，应等于参与数）'
    )

    # ---- 抽样 3 份：逐条列出确凿档与对应 fact id ------------------------------
    candidates = [
        int(row['person_id'])
        for row in db.execute("SELECT person_id FROM person_profile WHERE confirmed <> ''").fetchall()
    ]
    print(f'\n== 确凿档抽样（{min(3, len(candidates))}/{len(candidates)} 份非空） ==')
    for pid in random.Random(20260903).sample(candidates, min(3, len(candidates))):
        row = db.execute(
            'SELECT confirmed, summary, evidence_fingerprint FROM person_profile WHERE person_id = ?',
            (pid,),
        ).fetchone()
        print(f'-- person {pid}  指纹={str(row["evidence_fingerprint"])[:12]}…')
        for entry in parse_confirmed(str(row['confirmed'])):
            fact = db.execute(
                'SELECT slot, kind, content FROM facts WHERE id = ?', (entry.fact_id,),
            ).fetchone()
            marker = 'OK' if fact and str(fact['content']) == entry.content else '对不上'
            print(f'   [{marker}] fact#{entry.fact_id} ({entry.label}) {entry.content}')
        print(f'   印象：{row["summary"]}')

    # ---- 新旧对照：同一个人迁移前正文 vs 刷新后两档 ----------------------------
    changed = [
        pid for pid in person_ids
        if pre_summary.get(pid, '').strip() and profiles_for_injection(db, [pid])
    ]
    if changed:
        pid = changed[0]
        injected = profiles_for_injection(db, [pid])[0]
        print(f'\n== 新旧对照（person {pid}） ==')
        print(f'迁移前（自由文本整段）：{pre_summary[pid]}')
        print('刷新后：')
        for entry in injected.confirmed:
            print(f'   记着的 - {entry.label}：{entry.content}')
        print(f'   印象：{injected.impression}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='人物画像信任分级 · 真机副本验收')
    parser.add_argument('--db', required=True, help='真机库路径（只读，不改动）')
    parser.add_argument('--config', required=True, help='配置目录（含 providers/models 等四份 TOML）')
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == '__main__':
    raise SystemExit(main())
