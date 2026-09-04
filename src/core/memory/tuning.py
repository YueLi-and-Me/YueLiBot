"""检索调优中心：参数白名单、命名 profile 与弱监督评估。

四个部分：

1. **参数白名单**：允许调优的参数只有下表所列，其余检索链路上的数值
   （冻结阈值、半衰期、BM25 与向量的 0.4/0.6 混合等）不在调优范围内。
   白名单外的名字在构造 :class:`TuningOverrides` 时即被拒绝。
2. **profile**：一组参数覆盖的命名快照，落 ``retrieval_profiles`` 表
   （由链尾 DDL 幂等建表，同 ``jargon`` 先例，不占迁移号）。生效机制是
   进程内覆盖表：检索链路的读取点先查覆盖、没有再用配置初值，
   ``default`` profile 的覆盖集为空，等于现状。
3. **评估**：从 ``pipeline_events`` 与其指向的提示词转储抽弱监督样本，
   正例 = 进了提示词且该回合产生了回复的事实条目；重放检索按待评估参数
   排序后以 nDCG@k 与召回条数打分。评估期间临时切换进程内覆盖表，
   结束后原样还原；评估是同步调用，不与生产检索并发。
4. **第二轮评估（留痕重放）**：从 ``memory_retrieval_trace`` 留痕取生产
   当轮的真实检索词（当前文本与会话印象）复现两次召回并集，与留痕
   ``candidatePool`` 逐位对账；重排走生产 ``rank_recalled_facts`` 的向量
   融合。有留痕与无留痕的回合在报告里永远分成两个样本池，对不上留痕池
   的回合单独计数——那说明重放与生产仍有差异，其读数比 nDCG 本身重要。

弱监督的已知局限：正例本身来自当前参数的选择，评估度量的是
「换参数后既定选择的稳定性」，不是绝对相关性。该口径已于 2026-09-02
确认，不在此重开。
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.core.common.logger import get_logger
from src.core.observe import events as trace

logger = get_logger(__name__)

# 提示词转储中事实块的分节标记，与 agent/prompt.py 的渲染标题一致。
FACT_BLOCK_TAG = '长期记忆'
# 评估重放时每次召回取的候选池上限：远大于任何合法的 top-k，
# 保证 nDCG 的排名有足够深度容纳全部正例。
EVAL_POOL_LIMIT = 100
# 评估近似在场者时回看的消息条数。
PRESENCE_WINDOW_MESSAGES = 200

DEFAULT_PROFILE_NAME = 'default'
ACTIVE_PROFILE_META_KEY = 'retrieval_tuning_active_profile'


@dataclass(frozen=True)
class ParamSpec:
    """白名单里一个参数的规格。

    :ivar kind: 值类型，``int`` 或 ``float``。
    :ivar minimum: 取值下界（含）。
    :ivar maximum: 取值上界（含）。
    :ivar legacy: 现状值；``None`` 表示该参数现状由配置提供，无固定值。
    :ivar label: 展示名。
    """

    kind: type
    minimum: float
    maximum: float
    legacy: Optional[float]
    label: str


# 白名单本体。调优中心能碰的参数只有这些；新增条目要同步 WebUI 参数表。
WHITELIST: Dict[str, ParamSpec] = {
    'bm25_weight': ParamSpec(float, 0.1, 5.0, 1.0, '词面相关度权重'),
    'retention_weight_floor': ParamSpec(float, 0.0, 1.0, 0.35, '留存度权重下限'),
    'ppr_alpha': ParamSpec(float, 0.1, 0.99, 0.85, 'PPR 回迁概率'),
    'ppr_hops': ParamSpec(int, 1, 4, 2, 'PPR 跳数'),
    'pool_score_percentile': ParamSpec(float, 0.0, 0.9, 0.0, '候选池分数百分位'),
    'fact_recall_limit': ParamSpec(int, 1, 50, None, '进提示词的事实条数上限'),
    'recalled_episode_limit': ParamSpec(int, 0, 30, None, '情节召回条数'),
    'recent_episode_limit': ParamSpec(int, 0, 30, None, '近期情节条数'),
}


def validate_overrides(values: Mapping[str, Any]) -> Dict[str, Any]:
    """校验并规范化一组参数覆盖。

    :param values: 参数名到期望值的映射。
    :return: 白名单内、类型与范围都合法的覆盖表；空输入返回空表。
    :raises ValueError: 名字不在白名单、类型不符或越界时抛出，消息含违规项。
    副作用：无。
    """

    result: Dict[str, Any] = {}
    for name, value in values.items():
        spec = WHITELIST.get(name)
        if spec is None:
            raise ValueError(f'参数 {name} 不在调优白名单内')
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f'参数 {name} 需要数值，得到 {type(value).__name__}')
        if spec.kind is int and not isinstance(value, int):
            raise ValueError(f'参数 {name} 需要整数')
        number = float(value)
        if not spec.minimum <= number <= spec.maximum:
            raise ValueError(
                f'参数 {name} 越界：{value} 不在 [{spec.minimum}, {spec.maximum}]'
            )
        result[name] = spec.kind(value)
    return result


# ---------------------------------------------------------------- 进程内生效

# 当前生效的覆盖表。default profile 为空表，检索链路各读取点回落配置初值。
_active_overrides: Dict[str, Any] = {}


def set_active_overrides(values: Mapping[str, Any]) -> None:
    """替换进程内生效的参数覆盖表。

    :param values: 待生效的覆盖；先经 :func:`validate_overrides` 校验。
    :return: 无返回值。
    :raises ValueError: 覆盖不在白名单或越界。
    副作用：替换模块级覆盖表；对已产生的排序结果无追溯影响。
    """

    global _active_overrides
    _active_overrides = validate_overrides(values)


def active_overrides() -> Dict[str, Any]:
    """返回当前生效覆盖表的副本。"""

    return dict(_active_overrides)


def tuned_value(name: str, config_default: Any) -> Any:
    """读取一个白名单参数的当前生效值。

    :param name: 白名单参数名。
    :param config_default: 配置提供的初值；覆盖表未含该参数时使用。
    :return: 覆盖值或配置初值。
    副作用：无。
    """

    return _active_overrides.get(name, config_default)


def blend_score(relevance: float, retention_value: float) -> float:
    """按当前生效参数计算检索排序分。

    与 :func:`src.core.memory.decay.score` 的关系：两个权重取默认值
    （``bm25_weight=1.0``、``retention_weight_floor=0.35``）时与该函数
    逐字节等价；调优改的只是这两个系数，不改变乘法融合的形态。

    :param relevance: 词面相关度，通常已含向量融合。
    :param retention_value: 当前留存度。
    :return: 排序分数。
    副作用：读进程内覆盖表。
    """

    weight = float(_active_overrides.get('bm25_weight', 1.0))
    floor = float(_active_overrides.get('retention_weight_floor', 0.35))
    relevance = max(0.0, relevance)
    return (relevance ** weight) * (floor + (1.0 - floor) * retention_value)


def ppr_alpha() -> float:
    """读取当前生效的 PPR 回迁概率；未覆盖时取现状常量。"""

    return float(_active_overrides.get('ppr_alpha', 0.85))


def ppr_hops(default_hops: int) -> int:
    """读取当前生效的 PPR 跳数；未覆盖时沿用调用方现状值。"""

    return int(_active_overrides.get('ppr_hops', default_hops))


def pool_percentile() -> float:
    """读取候选池分数百分位过滤阈值；``0.0`` 表示不过滤（现状）。"""

    return float(_active_overrides.get('pool_score_percentile', 0.0))


def apply_pool_percentile(scores: Sequence[float]) -> int:
    """计算百分位过滤后应保留的候选条数。

    :param scores: 池内分数（顺序与池一致）。
    :return: 保留条数；阈值为 ``0`` 时返回全长。阈值对分数最低的尾部生效，
        因此保留的总是分数最高的一段前缀（池已按分数降序排列）。
    副作用：无。
    """

    threshold = pool_percentile()
    if threshold <= 0.0 or not scores:
        return len(scores)
    ordered = sorted(scores)
    cut = ordered[min(len(ordered) - 1, int(threshold * len(ordered)))]
    kept = sum(1 for value in scores if value >= cut)
    return max(kept, 1)


# ---------------------------------------------------------------- profile 落库


def list_profiles(db: sqlite3.Connection) -> List[Dict[str, Any]]:
    """列出全部已保存的 profile。

    :param db: 当前库连接。
    :return: 每项含 ``name``、``params``、``created_at``、``last_applied_at``，
        按名字排序；``default`` 永远在列（内置，参数为空覆盖）。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """

    rows = db.execute(
        'SELECT name, params, created_at, last_applied_at FROM retrieval_profiles'
        ' ORDER BY name'
    ).fetchall()
    profiles = [
        {
            'name': str(name),
            'params': json.loads(str(raw)),
            'created_at': int(created_at),
            'last_applied_at': int(applied) if applied is not None else None,
        }
        for name, raw, created_at, applied in rows
    ]
    if not any(item['name'] == DEFAULT_PROFILE_NAME for item in profiles):
        profiles.insert(0, {
            'name': DEFAULT_PROFILE_NAME,
            'params': {},
            'created_at': None,
            'last_applied_at': None,
        })
    return profiles


def save_profile(
    db: sqlite3.Connection,
    name: str,
    params: Mapping[str, Any],
    now: int,
) -> Dict[str, Any]:
    """保存或更新一个 profile。

    :param db: 当前库连接。
    :param name: profile 名；空串或 ``default`` 不允许保存（default 不可覆盖）。
    :param params: 参数覆盖；先经白名单校验。
    :param now: 当前毫秒时间戳。
    :return: 保存后的 profile 内容。
    :raises ValueError: 名字非法或参数不在白名单。
    :raises sqlite3.Error: 写入失败。
    副作用：UPSERT ``retrieval_profiles`` 并提交。
    """

    if not name.strip() or name == DEFAULT_PROFILE_NAME:
        raise ValueError(f'profile 名 {name!r} 不可保存：default 为内置项')
    overrides = validate_overrides(params)
    db.execute(
        '''INSERT INTO retrieval_profiles (name, params, created_at, last_applied_at)
           VALUES (?, ?, ?, NULL)
           ON CONFLICT(name) DO UPDATE SET params = excluded.params''',
        (name, json.dumps(overrides, ensure_ascii=False), now),
    )
    db.commit()
    logger.info('retrieval_profile_saved', profile=name, params=len(overrides))
    return {'name': name, 'params': overrides}


def delete_profile(db: sqlite3.Connection, name: str) -> None:
    """删除一个已保存的 profile；正在生效时一并回退到 default。

    :param db: 当前库连接。
    :param name: profile 名；``default`` 不可删除。
    :raises ValueError: 名字为内置项。
    :raises sqlite3.Error: 删除失败。
    副作用：删除行并提交；若删除的是当前生效项，进程内覆盖表清空、
        meta 中的生效名回到 ``default``。
    """

    if name == DEFAULT_PROFILE_NAME:
        raise ValueError('default 为内置 profile，不可删除')
    # 生效名判定必须在删行之前：行删掉后 active_profile_name 会把悬空名
    # 回落成 default，删后才比较会恒为假，进程内覆盖表就漏清了。
    was_active = active_profile_name(db) == name
    db.execute('DELETE FROM retrieval_profiles WHERE name = ?', (name,))
    db.commit()
    if was_active:
        _write_active_name(db, DEFAULT_PROFILE_NAME)
        set_active_overrides({})
    logger.info('retrieval_profile_deleted', profile=name)


def load_profile_params(db: sqlite3.Connection, name: str) -> Optional[Dict[str, Any]]:
    """读取一个 profile 的参数覆盖；不存在时返回 ``None``（default 恒为空表）。"""

    if name == DEFAULT_PROFILE_NAME:
        return {}
    row = db.execute(
        'SELECT params FROM retrieval_profiles WHERE name = ?', (name,)
    ).fetchone()
    if row is None:
        return None
    return validate_overrides(json.loads(str(row[0])))


def active_profile_name(db: sqlite3.Connection) -> str:
    """读取当前生效的 profile 名；meta 未设置或指向不存在的项时回落 default。"""

    row = db.execute(
        'SELECT value FROM meta WHERE key = ?', (ACTIVE_PROFILE_META_KEY,)
    ).fetchone()
    name = str(json.loads(str(row[0]))) if row is not None else DEFAULT_PROFILE_NAME
    if name != DEFAULT_PROFILE_NAME and load_profile_params(db, name) is None:
        return DEFAULT_PROFILE_NAME
    return name


def _write_active_name(db: sqlite3.Connection, name: str) -> None:
    db.execute(
        'INSERT INTO meta (key, value) VALUES (?, ?)'
        ' ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (ACTIVE_PROFILE_META_KEY, json.dumps(name, ensure_ascii=False)),
    )
    db.commit()


def apply_profile(db: sqlite3.Connection, name: str, now: int) -> Dict[str, Any]:
    """让一个 profile 生效：写 meta、记时间戳并替换进程内覆盖表。

    :param db: 当前库连接。
    :param name: profile 名；必须是已保存项或 ``default``。
    :param now: 当前毫秒时间戳。
    :return: ``{"profile": name, "params": 覆盖表}``。
    :raises ValueError: profile 不存在。
    :raises sqlite3.Error: 写入失败。
    副作用：更新 ``meta`` 与 ``retrieval_profiles.last_applied_at`` 并提交；
        替换进程内覆盖表；发出一条 ``retrieval_profile_applied`` 事件。
    """

    params = load_profile_params(db, name)
    if params is None:
        raise ValueError(f'profile {name!r} 不存在')
    set_active_overrides(params)
    _write_active_name(db, name)
    if name != DEFAULT_PROFILE_NAME:
        db.execute(
            'UPDATE retrieval_profiles SET last_applied_at = ? WHERE name = ?',
            (now, name),
        )
        db.commit()
    trace.emit('retrieval_profile_applied', profile=name, params=len(params))
    logger.info('retrieval_profile_applied', profile=name, params=len(params))
    return {'profile': name, 'params': params}


def rollback_to_default(db: sqlite3.Connection) -> Dict[str, Any]:
    """回滚到内置 default：清空覆盖，回到配置初值。"""

    return apply_profile(db, DEFAULT_PROFILE_NAME, 0)


def export_profile(db: sqlite3.Connection, name: str) -> Dict[str, Any]:
    """导出一个 profile 的可搬运内容。

    :param db: 当前库连接。
    :param name: profile 名。
    :return: ``{"profile": name, "params": {...}}``；default 返回空覆盖。
    :raises ValueError: profile 不存在。
    """

    params = load_profile_params(db, name)
    if params is None:
        raise ValueError(f'profile {name!r} 不存在')
    return {
        'profile': name,
        'params': params,
        'exported_at': datetime.now(timezone.utc).isoformat(),
    }


def bootstrap_active(db: sqlite3.Connection) -> str:
    """启动时按 meta 恢复进程内覆盖表，返回生效 profile 名供横幅展示。"""

    name = active_profile_name(db)
    params = load_profile_params(db, name) or {}
    set_active_overrides(params)
    return name


# ---------------------------------------------------------------- 评估


@dataclass
class TurnSample:
    """一个可评估的回合样本（弱监督）。

    :ivar turn_id: 事件账本中的回合 ID。
    :ivar stream_id: 会话 ID。
    :ivar stream_kind: 会话类型，决定可见性口径。
    :ivar at: 回合毫秒时间戳。
    :ivar query: 该回合的用户文本，作为重放检索词。
    :ivar present_person_ids: 近似在场者；评估时按其召回。
    :ivar positive_fact_ids: 进了提示词且该回合产生了回复的事实 ID。
    """

    turn_id: int
    stream_id: int
    stream_kind: str
    at: int
    query: str
    present_person_ids: List[int] = field(default_factory=list)
    positive_fact_ids: List[int] = field(default_factory=list)


def _fact_lines_from_dump(dump: Mapping[str, Any]) -> List[str]:
    """从转储的请求消息里取出事实块条目行。"""

    for message in dump.get('request', {}).get('messages', []):
        content = message.get('content', '')
        if isinstance(content, str) and content.startswith(f'[{FACT_BLOCK_TAG}]'):
            return [line[2:] for line in content.splitlines() if line.startswith('- ')]
    return []


def _match_fact_ids(db: sqlite3.Connection, lines: Sequence[str]) -> List[int]:
    """把渲染条目行匹配回事实 ID。

    条目行是 ``<人名> <正文>``（冲突时带槽位标注），正文才是稳定锚：
    正文整段出现在渲染行内即视为命中。渲染格式再有变化也只是漏配
    （该回合退出样本集），不会把正文错配到另一条事实上。

    :param db: 只读连接。
    :param lines: 渲染条目行。
    :return: 匹配到的事实 ID 列表；匹配不到的行丢弃。
    副作用：无。
    """

    rows = [
        (int(row[0]), str(row[1]).strip())
        for row in db.execute(
            'SELECT id, content FROM facts WHERE superseded_by IS NULL AND active = 1'
        ).fetchall()
    ]
    matched: List[int] = []
    seen: set[int] = set()
    for line in lines:
        for fact_id, content in rows:
            if fact_id in seen or not content:
                continue
            if content in line:
                matched.append(fact_id)
                seen.add(fact_id)
                break
    return matched


def build_turn_samples(
    db: sqlite3.Connection,
    *,
    max_turns: Optional[int] = None,
) -> List[TurnSample]:
    """从事件账本与提示词转储构建评估样本。

    样本条件（正例定义的前提）：planner 任务的 ``prompt_record``、
    转储文件可读、事实块至少匹配到一条、同一 ``turn_id`` 内产生了回复。
    同一回合只取首条合格转储：planner 在一个回合内可能因纠错重试被调用
    多次，逐条计入会把该回合的 nDCG 权重放大数倍。转储路径取自事件
    payload 本身，因此观察窗口受快照滚动保留量限制。

    :param db: 当前库连接。
    :param max_turns: 最多保留的样本数；省略时不限。
    :return: 按事件序排列的样本列表。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """

    kind_by_stream = {
        int(row[0]): str(row[1])
        for row in db.execute('SELECT id, kind FROM streams').fetchall()
    }
    replied_turns = {
        int(row[0])
        for row in db.execute(
            "SELECT DISTINCT turn_id FROM pipeline_events"
            " WHERE kind IN ('outbound_delivered', 'llm_final') AND turn_id IS NOT NULL"
        ).fetchall()
    }
    samples: List[TurnSample] = []
    seen_turns: set[int] = set()
    for at, stream_id, turn_id, payload in db.execute(
        "SELECT at, stream_id, turn_id, payload FROM pipeline_events"
        " WHERE kind = 'prompt_record' ORDER BY seq"
    ):
        if turn_id is None or int(turn_id) not in replied_turns:
            continue
        if int(turn_id) in seen_turns:
            continue
        try:
            task = json.loads(str(payload)).get('task')
            path = json.loads(str(payload)).get('path')
        except (TypeError, ValueError):
            continue
        if task != 'planner' or not path:
            continue
        dump_file = Path(str(path))
        if not dump_file.is_file():
            continue
        try:
            with open(dump_file, encoding='utf-8') as handle:
                dump = json.load(handle)
        except (OSError, ValueError):
            continue
        query = ''
        for message in dump.get('request', {}).get('messages', []):
            if message.get('role') == 'user' and message.get('content'):
                query = str(message.get('content'))
        lines = _fact_lines_from_dump(dump)
        if not query or not lines:
            continue
        positive = _match_fact_ids(db, lines)
        if not positive:
            continue
        present = [
            int(row[0]) for row in db.execute(
                'SELECT DISTINCT sender_person_id FROM messages'
                ' WHERE stream_id = ? AND created_at <= ? AND sender_person_id IS NOT NULL'
                ' ORDER BY id DESC LIMIT ?',
                (stream_id, at, PRESENCE_WINDOW_MESSAGES),
            ).fetchall()
        ]
        seen_turns.add(int(turn_id))
        samples.append(TurnSample(
            turn_id=int(turn_id),
            stream_id=int(stream_id),
            stream_kind=kind_by_stream.get(int(stream_id), 'direct'),
            at=int(at),
            query=query,
            present_person_ids=present,
            positive_fact_ids=positive,
        ))
        if max_turns is not None and len(samples) >= max_turns:
            break
    return samples


def ndcg_at_k(ranked_ids: Sequence[int], positive_ids: Sequence[int], k: int) -> float:
    """计算单回合 nDCG@k。

    :param ranked_ids: 重放排序后的候选 ID 序列（分数降序）。
    :param positive_ids: 该回合的正例 ID。
    :param k: 截断深度。
    :return: ``[0, 1]``；正例为空时返回 ``0.0``。
    副作用：无。
    """

    positives = set(positive_ids)
    if not positives or k <= 0:
        return 0.0
    dcg = sum(
        1.0 / math.log2(position + 2)
        for position, fact_id in enumerate(ranked_ids[:k])
        if fact_id in positives
    )
    ideal = sum(
        1.0 / math.log2(position + 2)
        for position in range(min(len(positives), k))
    )
    return dcg / ideal if ideal > 0 else 0.0


@dataclass
class EvalReport:
    """一次评估的汇总结果。

    :ivar profile: 被评估的 profile 名；匿名参数集为 ``None``。
    :ivar params: 本次评估实际生效的覆盖表。
    :ivar k: nDCG 截断深度（进提示词条数上限）。
    :ivar sample_count: 样本数。
    :ivar ndcg_mean: 各回合 nDCG@k 的算术平均。
    :ivar recall_count_median / mean: 重放下进入提示词条数的中位与均值。
    :ivar displaced_positive_total: 正例落在前 k 之外的总条数——
        「该进的有没有被挤掉」的直接读数。
    :ivar per_turn: 每回合一行明细。
    :ivar generated_at: 报告生成时刻。
    """

    profile: Optional[str]
    params: Dict[str, Any]
    k: int
    sample_count: int
    ndcg_mean: float
    recall_count_median: float
    recall_count_mean: float
    displaced_positive_total: int
    per_turn: List[Dict[str, Any]] = field(default_factory=list)
    generated_at: str = ''

    def as_dict(self) -> Dict[str, Any]:
        """序列化为 API 响应友好的字典。"""

        return {
            'profile': self.profile,
            'params': self.params,
            'k': self.k,
            'sample_count': self.sample_count,
            'ndcg_mean': round(self.ndcg_mean, 4),
            'recall_count_median': self.recall_count_median,
            'recall_count_mean': round(self.recall_count_mean, 2),
            'displaced_positive_total': self.displaced_positive_total,
            'per_turn': self.per_turn,
            'generated_at': self.generated_at,
        }


def evaluate(
    store: Any,
    samples: Sequence[TurnSample],
    overrides: Mapping[str, Any],
    *,
    profile_name: Optional[str] = None,
    config_fact_limit: int = 6,
) -> EvalReport:
    """按给定参数重放检索并输出 nDCG@k 与召回条数。

    评估期间临时替换进程内覆盖表，结束后还原；重放只读，
    不强化、不回补、不写库。

    :param store: MemoryStore 实例。
    :param samples: :func:`build_turn_samples` 的产物。
    :param overrides: 本次评估的参数覆盖（先经白名单校验）。
    :param profile_name: 来源 profile 名；匿名评估为 ``None``。
    :param config_fact_limit: 配置提供的进提示词条数初值。
    :return: 汇总报告。
    :raises ValueError: 参数不在白名单。
    副作用：临时切换并还原进程内覆盖表；发出一条 ``retrieval_eval_done`` 事件。
    """

    evaluated = validate_overrides(overrides)
    previous = active_overrides()
    per_turn: List[Dict[str, Any]] = []
    displaced_total = 0
    try:
        set_active_overrides(evaluated)
        k = int(tuned_value('fact_recall_limit', config_fact_limit))
        for sample in samples:
            pool = store.recall_facts_in_scope(
                sample.present_person_ids,
                sample.query,
                EVAL_POOL_LIMIT,
                now=sample.at,
                stream_kind=sample.stream_kind,
                return_candidates=True,
            )
            ranked = sorted(pool, key=lambda fact: fact.score, reverse=True)
            ranked_ids = [fact.id for fact in ranked[:k]]
            value = ndcg_at_k(ranked_ids, sample.positive_fact_ids, k)
            displaced = sum(
                1 for fact_id in sample.positive_fact_ids if fact_id not in set(ranked_ids)
            )
            displaced_total += displaced
            per_turn.append({
                'turn_id': sample.turn_id,
                'stream_kind': sample.stream_kind,
                'positive_count': len(sample.positive_fact_ids),
                'recall_count': len(ranked_ids),
                'ndcg': round(value, 4),
                'displaced_positive': displaced,
            })
    finally:
        set_active_overrides(previous)

    sample_count = len(per_turn)
    ndcg_values = [row['ndcg'] for row in per_turn]
    recalls = [row['recall_count'] for row in per_turn]
    report = EvalReport(
        profile=profile_name,
        params=evaluated,
        k=k,
        sample_count=sample_count,
        ndcg_mean=(sum(ndcg_values) / sample_count) if sample_count else 0.0,
        recall_count_median=(sorted(recalls)[len(recalls) // 2] if recalls else 0),
        recall_count_mean=(sum(recalls) / sample_count) if sample_count else 0.0,
        displaced_positive_total=displaced_total,
        per_turn=per_turn,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    trace.emit(
        'retrieval_eval_done',
        profile=profile_name or '',
        samples=sample_count,
        ndcgMean=round(report.ndcg_mean, 4),
    )
    logger.info(
        'retrieval_eval_done',
        profile=profile_name or 'anonymous',
        samples=sample_count,
        ndcg=round(report.ndcg_mean, 4),
        displaced=displaced_total,
    )
    return report


# ---------------------------------------------------------------- 第二轮评估：留痕重放

# 留痕对账的分数比较容差。两侧分数来自同一组浮点表达式，输入一致时逐位
# 相同；容差只吸收浮点环境的表示差，不掩盖任何排序级差异。
RECONCILE_SCORE_EPSILON = 1e-12
# 近似在场者回看的消息条数；与生产 ``recent_speakers`` 的默认扫描上界一致。
PRESENCE_SCAN_LIMIT = 200


@dataclass
class TraceSample:
    """一条召回留痕（``memory_retrieval_trace`` 事件）对应的可重放回合。

    :ivar turn_id: 事件账本中的回合 ID；留痕未关联回合时为 ``None``。
    :ivar stream_id: 会话 ID。
    :ivar stream_kind: 会话类型，取重放当时的 ``streams.kind``。
    :ivar at: 留痕事件毫秒时间戳，兼作重放的衰减时钟。
    :ivar current_text: 生产当轮的当前文本检索词。
    :ivar impression: 生产当轮的会话印象检索词；未生成印象时为空串。
    :ivar candidate_pool: 生产词面阶段的候选池，``(factId, score)`` 按序。
    :ivar prompt_fact_ids: 首份生产提示词实际使用的事实 ID。
    :ivar replied: 该回合是否产生了回复；无法判定时为 ``False``。
    """

    turn_id: Optional[int]
    stream_id: int
    stream_kind: str
    at: int
    current_text: str
    impression: str
    candidate_pool: Tuple[Tuple[int, float], ...]
    prompt_fact_ids: List[int] = field(default_factory=list)
    replied: bool = False

    @property
    def scorable(self) -> bool:
        """该回合能否进入 nDCG 统计：正例非空且确实产生了回复。"""

        return bool(self.prompt_fact_ids) and self.replied


def build_trace_samples(
    db: sqlite3.Connection,
    *,
    max_turns: Optional[int] = None,
) -> List[TraceSample]:
    """从召回留痕事件构建重放样本。

    全部留痕都进样本：对账不依赖正例，``promptFactIds`` 为空的回合同样要
    比对候选池。同一回合只取首条留痕（生产侧同轮只发一条，这里按回合去重
    兜底）。留痕事件不存在时返回空列表，调用方据此把回合划入无留痕样本池。

    :param db: 当前库连接。
    :param max_turns: 最多保留的样本数；省略时不限。
    :return: 按事件序排列的样本列表。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """

    kind_by_stream = {
        int(row[0]): str(row[1])
        for row in db.execute('SELECT id, kind FROM streams').fetchall()
    }
    replied_turns = {
        int(row[0])
        for row in db.execute(
            "SELECT DISTINCT turn_id FROM pipeline_events"
            " WHERE kind IN ('outbound_delivered', 'llm_final') AND turn_id IS NOT NULL"
        ).fetchall()
    }
    samples: List[TraceSample] = []
    seen_turns: set[int] = set()
    for seq, at, stream_id, turn_id, payload in db.execute(
        "SELECT seq, at, stream_id, turn_id, payload FROM pipeline_events"
        " WHERE kind = 'memory_retrieval_trace' ORDER BY seq"
    ):
        key = int(turn_id) if turn_id is not None else -int(seq)
        if key in seen_turns:
            continue
        try:
            data = json.loads(str(payload))
        except (TypeError, ValueError):
            continue
        pool: List[Tuple[int, float]] = []
        for item in data.get('candidatePool') or []:
            if isinstance(item, dict) and item.get('factId') is not None:
                pool.append((int(item['factId']), float(item.get('score', 0.0))))
        prompt_ids: List[int] = []
        for value in data.get('promptFactIds') or []:
            try:
                prompt_ids.append(int(value))
            except (TypeError, ValueError):
                continue
        seen_turns.add(key)
        samples.append(TraceSample(
            turn_id=int(turn_id) if turn_id is not None else None,
            stream_id=int(stream_id),
            stream_kind=kind_by_stream.get(int(stream_id), 'direct'),
            at=int(at),
            current_text=str(data.get('currentText') or ''),
            impression=str(data.get('conversationImpression') or ''),
            candidate_pool=tuple(pool),
            prompt_fact_ids=prompt_ids,
            replied=(int(turn_id) in replied_turns) if turn_id is not None else False,
        ))
        if max_turns is not None and len(samples) >= max_turns:
            break
    return samples


def approximate_present_persons(
    store: Any,
    stream_id: int,
    at: int,
) -> List[int]:
    """按事件时刻近似生产在场者。

    生产在场者 = 当前说话人 + ``recent_speakers(回合水位)``。留痕未记这组
    ID，重放以「事件时刻前的最大消息 ID」为水位、以该水位前最近一条用户
    消息的发送者为当前说话人，其余在场者复用 ``recent_speakers`` 本身。
    近似与生产的偏差由留痕对账计数体现，不在此修正。

    :param store: MemoryStore 实例。
    :param stream_id: 会话 ID。
    :param at: 留痕事件毫秒时间戳。
    :return: 去重后的人物主键，当前说话人排在最前；无消息时为空列表。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """

    watermark_row = store._db.execute(
        'SELECT MAX(id) FROM messages WHERE stream_id = ? AND created_at <= ?',
        (stream_id, at),
    ).fetchone()
    watermark = int(watermark_row[0]) if watermark_row and watermark_row[0] else 0
    if not watermark:
        return []
    speaker_row = store._db.execute(
        'SELECT sender_person_id FROM messages'
        ' WHERE stream_id = ? AND id <= ? AND sender_person_id IS NOT NULL'
        ' ORDER BY id DESC LIMIT 1',
        (stream_id, watermark),
    ).fetchone()
    if speaker_row is None:
        return store.recent_speakers(stream_id, watermark, PRESENCE_SCAN_LIMIT)
    speaker = int(speaker_row[0])
    return [speaker] + [
        person_id for person_id in store.recent_speakers(
            stream_id, watermark, PRESENCE_SCAN_LIMIT
        )
        if person_id != speaker
    ]


def replay_union_recall(
    store: Any,
    person_ids: Sequence[int],
    current_text: str,
    impression: str,
    limit: int,
    now: int,
    *,
    stream_kind: str,
    private_in_group: bool = False,
) -> list:
    """复现回合组装期的两次召回并集，作为融合重排的候选池。

    与 ``chat._recall_turn_facts`` 同构：当前文本先入池、按 ID 去重、按分数
    稳定排序（词面分相同时当前文本候选在前）、按候选池百分位截尾。``limit``
    取生产同源的进提示词条数上限——召回入口内部按 ``limit * 3`` 截取 FTS
    候选，传更大的池上限会使重放池大于生产池。

    :param store: MemoryStore 实例。
    :param person_ids: 在场者近似。
    :param current_text: 留痕的当前文本检索词。
    :param impression: 留痕的会话印象检索词；空串表示只用当前文本。
    :param limit: 进提示词条数上限（与生产同参）。
    :param now: 留痕事件毫秒时间戳。
    :param stream_kind: 会话类型。
    :param private_in_group: ``conversation.private_facts_in_group`` 的当前值。
    :return: 词面阶段候选池，按分数降序。
    :raises sqlite3.Error: 检索失败。
    副作用：只读；被可见性规则挡下时与生产同源发出事件。
    """

    pool: list = []
    seen: set[int] = set()
    for text in (current_text, impression):
        if not text:
            continue
        for fact in store.recall_facts_in_scope(
            person_ids, text, limit, now,
            stream_kind=stream_kind,
            private_in_group=private_in_group,
            return_candidates=True,
        ):
            if fact.id not in seen:
                seen.add(fact.id)
                pool.append(fact)
    pool.sort(key=lambda fact: fact.score, reverse=True)
    kept = apply_pool_percentile([fact.score for fact in pool])
    return pool[:kept]


@dataclass
class ReconcileResult:
    """一次留痕对账的比对结果。

    :ivar status: ``match`` 或 ``mismatch``；ID 序列逐位一致才为 ``match``。
    :ivar missing: 留痕池有、重放池没有的事实 ID。
    :ivar extra: 重放池有、留痕池没有的事实 ID。
    :ivar order_changed: 集合相同而顺序不同。
    :ivar max_score_delta: 同一事实在两侧分数的最大绝对差；无公共事实时为
        ``None``。
    """

    status: str
    missing: List[int] = field(default_factory=list)
    extra: List[int] = field(default_factory=list)
    order_changed: bool = False
    max_score_delta: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        """序列化为报告行友好的字典。"""

        return {
            'status': self.status,
            'missing': self.missing,
            'extra': self.extra,
            'orderChanged': self.order_changed,
            'maxScoreDelta': (
                None if self.max_score_delta is None
                else round(self.max_score_delta, 6)
            ),
        }


def reconcile_trace_pool(
    replay_pool: Sequence[Any],
    trace_pool: Sequence[Tuple[int, float]],
) -> ReconcileResult:
    """把重放候选池与留痕 ``candidatePool`` 逐位对账。

    对不上的定义取最强口径：两侧 ID 序列逐位一致才算 ``match``。集合差异
    （事实被取代、冻结或新增）与顺序差异（分数漂移）分开报告，分数偏差
    单独给最大值，不与集合判定混在一处。

    :param replay_pool: 重放候选池，含 ``id`` 与 ``score`` 属性的对象序列。
    :param trace_pool: 留痕候选池，``(factId, score)`` 序列。
    :return: 对账结果。
    副作用：无。
    """

    replay_ids = [fact.id for fact in replay_pool]
    replay_scores = {fact.id: float(fact.score) for fact in replay_pool}
    trace_ids = [int(fact_id) for fact_id, _ in trace_pool]
    trace_scores = {int(fact_id): float(score) for fact_id, score in trace_pool}
    missing = [fact_id for fact_id in trace_ids if fact_id not in replay_scores]
    extra = [fact_id for fact_id in replay_ids if fact_id not in trace_scores]
    common = set(replay_scores) & set(trace_scores)
    delta = (
        max(abs(replay_scores[fact_id] - trace_scores[fact_id]) for fact_id in common)
        if common else None
    )
    order_changed = (
        not missing and not extra and replay_ids != trace_ids
    )
    matched = (
        replay_ids == trace_ids
        and (delta is None or delta <= RECONCILE_SCORE_EPSILON)
    )
    return ReconcileResult(
        status='match' if matched else 'mismatch',
        missing=missing,
        extra=extra,
        order_changed=order_changed,
        max_score_delta=delta,
    )


def _aggregate_stream_groups(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把回合明细按会话类型聚成三列：整体 / 群聊 / 私聊。

    桌面会话计入整体列，不单列；样本数不足以支撑独立一列时单列只会放大
    抖动。nDCG 与被挤掉正例只在可评分回合上统计，召回条数在全部回合上
    统计——后者度量检索行为本身，与正例无关。

    :param rows: 回合明细行，含 ``stream_kind`` / ``scorable`` / ``ndcg`` /
        ``recall_count`` / ``displaced_positive``。
    :return: ``{'all': ..., 'group': ..., 'direct': ...}``。
    副作用：无。
    """

    def aggregate(subset: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        scorable = [row for row in subset if row['scorable']]
        ndcg_values = [row['ndcg'] for row in scorable]
        recalls = [row['recall_count'] for row in subset]
        return {
            'sample_count': len(subset),
            'scorable_count': len(scorable),
            'ndcg_mean': round(sum(ndcg_values) / len(ndcg_values), 4) if ndcg_values else 0.0,
            'recall_count_median': sorted(recalls)[len(recalls) // 2] if recalls else 0,
            'recall_count_mean': round(sum(recalls) / len(recalls), 2) if recalls else 0.0,
            'displaced_positive_total': sum(
                row['displaced_positive'] for row in scorable
            ),
        }

    return {
        'all': aggregate(rows),
        'group': aggregate([r for r in rows if r['stream_kind'] == 'group']),
        'direct': aggregate([r for r in rows if r['stream_kind'] == 'direct']),
    }


@dataclass
class Round2Report:
    """第二轮评估的汇总结果。

    :ivar profile: 被评估的 profile 名；匿名参数集为 ``None``。
    :ivar params: 本次评估实际生效的覆盖表。
    :ivar k: nDCG 截断深度（进提示词条数上限）。
    :ivar traced: 留痕样本池的汇总（含对账）。
    :ivar untraced: 无留痕样本池的汇总。
    :ivar embedding: 查询向量成本：调用次数、缓存命中与累计耗时毫秒。
    :ivar per_turn: 每回合一行明细，两个池的行混排、以 ``pool`` 字段区分。
    :ivar generated_at: 报告生成时刻。
    """

    profile: Optional[str]
    params: Dict[str, Any]
    k: int
    traced: Dict[str, Any]
    untraced: Dict[str, Any]
    embedding: Dict[str, Any]
    per_turn: List[Dict[str, Any]] = field(default_factory=list)
    generated_at: str = ''

    def as_dict(self) -> Dict[str, Any]:
        """序列化为 API 响应友好的字典。"""

        return {
            'profile': self.profile,
            'params': self.params,
            'k': self.k,
            'traced': self.traced,
            'untraced': self.untraced,
            'embedding': self.embedding,
            'per_turn': self.per_turn,
            'generated_at': self.generated_at,
        }


async def evaluate_round2(
    store: Any,
    traced: Sequence[TraceSample],
    untraced: Sequence[TurnSample],
    overrides: Mapping[str, Any],
    *,
    embed_query: Optional[Callable[[str], Awaitable[Optional[bytes]]]] = None,
    profile_name: Optional[str] = None,
    config_fact_limit: int = 6,
    private_in_group: bool = False,
) -> Round2Report:
    """按留痕检索词重放召回、并入向量融合重排，输出按会话类型分组的报告。

    两个样本池在报告里永远分开：留痕池用留痕检索词复现两次召回并集并与
    ``candidatePool`` 对账；无留痕池沿用提示词转储反推的当前文本，检索词
    近似造成的偏差继续存在，数字不可与留痕池混读。融合重排直接调用生产
    的 ``rank_recalled_facts``，查询向量按生产口径取当前文本。

    查询向量按文本缓存：同一文本只调用一次 ``embed_query``，实际调用次数
    与累计耗时记入报告。``embed_query`` 为 ``None`` 时融合退化为词面排序，
    与生产在向量服务未装配时的行为一致。

    评估期间临时替换进程内覆盖表，结束后原样还原；重放只读，
    不强化、不回补、不写库。

    :param store: MemoryStore 实例。
    :param traced: :func:`build_trace_samples` 的产物。
    :param untraced: :func:`build_turn_samples` 的产物（无留痕回合）。
    :param overrides: 本次评估的参数覆盖（先经白名单校验）。
    :param embed_query: 异步查询向量函数，签名与 ``VectorService.embed_query``
        一致；省略时融合退化。
    :param profile_name: 来源 profile 名；匿名评估为 ``None``。
    :param config_fact_limit: 配置提供的进提示词条数初值。
    :param private_in_group: ``conversation.private_facts_in_group`` 的当前值。
    :return: 汇总报告。
    :raises ValueError: 参数不在白名单。
    副作用：临时切换并还原进程内覆盖表；发出一条 ``retrieval_eval_round2_done``
        事件；对账期间可见性规则挡下条目时会发与生产同源的事件。
    """

    evaluated = validate_overrides(overrides)
    previous = active_overrides()
    per_turn: List[Dict[str, Any]] = []
    vector_cache: Dict[str, Optional[bytes]] = {}
    embed_calls = 0
    embed_cache_hits = 0
    embed_elapsed_ms = 0.0

    async def query_vector(text: str) -> Optional[bytes]:
        nonlocal embed_calls, embed_cache_hits, embed_elapsed_ms
        if embed_query is None:
            return None
        if text in vector_cache:
            embed_cache_hits += 1
            return vector_cache[text]
        embed_calls += 1
        started = monotonic()
        try:
            vector = await embed_query(text)
        finally:
            embed_elapsed_ms += (monotonic() - started) * 1000.0
        # 失败结果（None）同样缓存：生产对失败的查询向量退回词面排序，
        # 重试只放大评估成本，不改变口径。
        vector_cache[text] = vector
        return vector

    def record_turn(
        *,
        pool: str,
        turn_id: Optional[int],
        stream_kind: str,
        reconcile: Optional[ReconcileResult],
        prompt_fact_ids: Sequence[int],
        ranked_ids: Sequence[int],
        recall_source_count: int,
    ) -> None:
        # 正例是否进入 nDCG 统计由调用方按各池口径先行判定（留痕池见
        # TraceSample.scorable，无留痕池进样本即有正例），这里只看正例
        # 是否非空。
        scorable = bool(prompt_fact_ids)
        value = ndcg_at_k(ranked_ids, prompt_fact_ids, k) if prompt_fact_ids else 0.0
        displaced = (
            sum(1 for fact_id in prompt_fact_ids if fact_id not in set(ranked_ids))
            if prompt_fact_ids else 0
        )
        per_turn.append({
            'turn_id': turn_id,
            'pool': pool,
            'stream_kind': stream_kind,
            'scorable': scorable,
            'reconcile': reconcile.as_dict() if reconcile is not None else None,
            'positive_count': len(prompt_fact_ids),
            'candidate_count': recall_source_count,
            'recall_count': len(ranked_ids),
            'ranked_ids': list(ranked_ids),
            'ndcg': round(value, 4),
            'displaced_positive': displaced,
        })

    try:
        set_active_overrides(evaluated)
        k = int(tuned_value('fact_recall_limit', config_fact_limit))
        for sample in traced:
            present = approximate_present_persons(store, sample.stream_id, sample.at)
            replay = replay_union_recall(
                store, present, sample.current_text, sample.impression,
                k, sample.at,
                stream_kind=sample.stream_kind,
                private_in_group=private_in_group,
            )
            reconcile = reconcile_trace_pool(replay, sample.candidate_pool)
            query_embedding = await query_vector(sample.current_text)
            ranked = store.rank_recalled_facts(replay, query_embedding, k)
            record_turn(
                pool='traced',
                turn_id=sample.turn_id,
                stream_kind=sample.stream_kind,
                reconcile=reconcile,
                prompt_fact_ids=sample.prompt_fact_ids if sample.scorable else [],
                ranked_ids=[fact.id for fact in ranked],
                recall_source_count=len(replay),
            )
        for sample in untraced:
            present = approximate_present_persons(store, sample.stream_id, sample.at)
            replay = replay_union_recall(
                store, present, sample.query, '',
                k, sample.at,
                stream_kind=sample.stream_kind,
                private_in_group=private_in_group,
            )
            query_embedding = await query_vector(sample.query)
            ranked = store.rank_recalled_facts(replay, query_embedding, k)
            record_turn(
                pool='untraced',
                turn_id=sample.turn_id,
                stream_kind=sample.stream_kind,
                reconcile=None,
                prompt_fact_ids=sample.positive_fact_ids,
                ranked_ids=[fact.id for fact in ranked],
                recall_source_count=len(replay),
            )
    finally:
        set_active_overrides(previous)

    traced_rows = [row for row in per_turn if row['pool'] == 'traced']
    untraced_rows = [row for row in per_turn if row['pool'] == 'untraced']
    reconciles = [row['reconcile'] for row in traced_rows]

    def pool_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            'sample_count': len(rows),
            'groups': _aggregate_stream_groups(rows),
        }

    report = Round2Report(
        profile=profile_name,
        params=evaluated,
        k=k,
        traced={
            **pool_summary(traced_rows),
            'reconcile_matched': sum(
                1 for item in reconciles if item and item['status'] == 'match'
            ),
            'reconcile_mismatched': sum(
                1 for item in reconciles if item and item['status'] == 'mismatch'
            ),
        },
        untraced=pool_summary(untraced_rows),
        embedding={
            'calls': embed_calls,
            'cache_hits': embed_cache_hits,
            'elapsed_ms': round(embed_elapsed_ms, 1),
        },
        per_turn=per_turn,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    trace.emit(
        'retrieval_eval_round2_done',
        profile=profile_name or '',
        traced=len(traced_rows),
        untraced=len(untraced_rows),
        reconcileMismatched=report.traced['reconcile_mismatched'],
        embeddingCalls=embed_calls,
    )
    logger.info(
        'retrieval_eval_round2_done',
        profile=profile_name or 'anonymous',
        traced=len(traced_rows),
        untraced=len(untraced_rows),
        reconcile_mismatched=report.traced['reconcile_mismatched'],
        embedding_calls=embed_calls,
    )
    return report
