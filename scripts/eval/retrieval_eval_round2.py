"""真机库副本上的检索评估第二迭代跑数脚本。

流程：
1. 把真机 data/memory.db 拷到系统临时目录（原库只读）；
2. 复现旧口径（BM25-only、提示词转储反推检索词），对照 0.2875 基线；
3. 跑第二轮评估：留痕池（memory_retrieval_trace 重放 + 对账）与
   无留痕池分开，向量融合同生产口径，按 stream_kind 三列分组；
4. 汇总打印验收数字，明细落 JSON。

用法：.venv/Scripts/python.exe scripts/eval/retrieval_eval_round2.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from src.core.config.loader import load_config
from src.core.llm_models.router import create_routers
from src.core.memory import tuning
from src.core.memory.embed import build_client
from src.core.memory.store import MemoryStore
from src.core.observe.store import event_store

# 默认读本仓库的运行库；从 worktree 里跑时用 YUELI_EVAL_DB 指向主树那份。
# 原写法是 REPO.parent.parent / 'YueLiBot' / 'data'，把目录名写死成 YueLiBot，
# 换个克隆目录名就找不到库，且属于不该随代码分发的本机路径假设。
LIVE_DB = Path(os.environ.get('YUELI_EVAL_DB') or REPO / 'data' / 'memory.db')
# 评估产物落 data/（已在 .gitignore 内），不再写回测试目录
OUT_JSON = REPO / 'data' / 'eval' / 'retrieval_eval_round2_result.json'


def _copy_live_db(workdir: Path) -> Path:
    """用 backup API 取真机库的一致性快照。

    真机进程在写，直接拷主文件会丢 -wal 里尚未合并的事件；backup 走
    SQLite 自身的快照读，副本与拷贝时刻的库内容一致。
    """

    source = sqlite3.connect(f'file:{LIVE_DB}?mode=ro', uri=True)
    target_path = workdir / 'memory.db'
    dest = sqlite3.connect(target_path)
    with dest:
        source.backup(dest)
    source.close()
    dest.close()
    return target_path


def _build_embed_query(cfg):
    routers = create_routers(cfg)
    if not routers.embedding.ready:
        print('!! embedding 路由不可用，融合将退化为词面排序')
        return None
    client = build_client(routers.embedding)
    print(f'embedding 模型: {routers.embedding.model}')

    async def embed(text: str) -> bytes | None:
        return await client.embed_one(text)

    return embed


async def main(dry_run: bool) -> None:
    started = time.monotonic()
    cfg = load_config(REPO / 'config')
    config_limit = cfg.conversation.fact_recall_limit
    private_in_group = cfg.conversation.private_facts_in_group
    print(f'config: fact_recall_limit={config_limit} private_in_group={private_in_group}')

    workdir = Path(tempfile.mkdtemp(prefix='yuelibot-eval2-'))
    # 评估自身发出的事件只落临时事件库，不写副本、不写真机。
    event_store.configure(workdir / 'eval-events.db')
    db_path = _copy_live_db(workdir)
    print(f'真机库副本: {db_path}')
    con = sqlite3.connect(db_path)
    store = MemoryStore(con)
    version = con.execute('PRAGMA user_version').fetchone()[0]
    print(f'副本 user_version = {version}')

    untraced = tuning.build_turn_samples(con)
    traced = tuning.build_trace_samples(con)
    print(f'无留痕样本: {len(untraced)} 回合；有留痕样本: {len(traced)} 回合')

    # ---- 旧口径复现（对照 0.2875）：不改任何输入。 ----
    legacy = tuning.evaluate(
        store, untraced, {},
        config_fact_limit=config_limit,
    )
    print(
        f'[旧口径复现] samples={legacy.sample_count} k={legacy.k}'
        f' nDCG@{legacy.k}={legacy.ndcg_mean:.4f}'
        f' displaced={legacy.displaced_positive_total}'
        f' positives={sum(r["positive_count"] for r in legacy.per_turn)}'
    )

    # ---- 第二轮：双池 + 对账 + 融合 + 分组。 ----
    embed_query = None if dry_run else _build_embed_query(cfg)
    report = await tuning.evaluate_round2(
        store, traced, untraced, {},
        embed_query=embed_query,
        config_fact_limit=config_limit,
        private_in_group=private_in_group,
    )
    for pool_name in ('traced', 'untraced'):
        pool = report.as_dict()[pool_name]
        groups = pool['groups']
        line = (
            f"[{pool_name}] samples={pool['sample_count']}"
            f" | all: n={groups['all']['sample_count']}"
            f" scorable={groups['all']['scorable_count']}"
            f" nDCG={groups['all']['ndcg_mean']:.4f}"
            f" displaced={groups['all']['displaced_positive_total']}"
            f" | group: n={groups['group']['sample_count']}"
            f" nDCG={groups['group']['ndcg_mean']:.4f}"
            f" displaced={groups['group']['displaced_positive_total']}"
            f" | direct: n={groups['direct']['sample_count']}"
            f" nDCG={groups['direct']['ndcg_mean']:.4f}"
            f" displaced={groups['direct']['displaced_positive_total']}"
        )
        print(line)
        if pool_name == 'traced':
            print(
                f"[traced 对账] matched={pool['reconcile_matched']}"
                f" mismatched={pool['reconcile_mismatched']}"
                f" / {pool['sample_count']}"
            )
    positives_traced = sum(
        r['positive_count'] for r in report.per_turn
        if r['pool'] == 'traced' and r['scorable']
    )
    displaced_traced = sum(
        r['displaced_positive'] for r in report.per_turn
        if r['pool'] == 'traced'
    )
    positives_untraced = sum(
        r['positive_count'] for r in report.per_turn
        if r['pool'] == 'untraced'
    )
    displaced_untraced = sum(
        r['displaced_positive'] for r in report.per_turn
        if r['pool'] == 'untraced'
    )
    print(
        f'[正例/挤掉] traced: {displaced_traced}/{positives_traced}'
        f' untraced: {displaced_untraced}/{positives_untraced}'
    )
    print(
        f"[embedding] calls={report.embedding['calls']}"
        f" cache_hits={report.embedding['cache_hits']}"
        f" elapsed_ms={report.embedding['elapsed_ms']}"
    )

    payload = {
        'legacy': legacy.as_dict(),
        'round2': report.as_dict(),
        'wall_seconds': round(time.monotonic() - started, 1),
        'db_user_version': version,
        'config': {
            'fact_recall_limit': config_limit,
            'private_facts_in_group': private_in_group,
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8'
    )
    print(f'明细已写入 {OUT_JSON}（墙钟 {payload["wall_seconds"]}s）')
    con.close()


if __name__ == '__main__':
    asyncio.run(main(dry_run='--dry' in sys.argv))
