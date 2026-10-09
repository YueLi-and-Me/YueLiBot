"""真机库副本上的检索评估（留痕重放）跑数脚本。

流程：
1. 把真机 data/memory.db 拷到系统临时目录（原库只读）；
2. 重放 memory_retrieval_trace 留痕并与 candidatePool 对账，向量融合同生产
   口径，按 stream_kind 三列分组；
3. 汇总打印验收数字，明细落 JSON。

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

    traced = tuning.build_trace_samples(con)
    print(f'留痕样本: {len(traced)} 回合')

    # ---- 留痕重放 + 对账 + 融合 + 分组。 ----
    embed_query = None if dry_run else _build_embed_query(cfg)
    report = await tuning.evaluate_round2(
        store, traced, {},
        embed_query=embed_query,
        config_fact_limit=config_limit,
        private_in_group=private_in_group,
    )
    pool = report.traced
    groups = pool['groups']
    print(
        f"[traced] samples={pool['sample_count']}"
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
    print(
        f"[traced 对账] matched={pool['reconcile_matched']}"
        f" mismatched={pool['reconcile_mismatched']}"
        f" / {pool['sample_count']}"
    )
    # 不可评分回合的 positive_count 与 displaced_positive 恒为 0，直接全量求和。
    positives = sum(r['positive_count'] for r in report.per_turn)
    displaced = sum(r['displaced_positive'] for r in report.per_turn)
    print(f'[正例/挤掉] {displaced}/{positives}')
    print(
        f"[embedding] calls={report.embedding['calls']}"
        f" cache_hits={report.embedding['cache_hits']}"
        f" elapsed_ms={report.embedding['elapsed_ms']}"
    )

    payload = {
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
