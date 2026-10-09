"""检索调优中心：参数白名单、命名 profile 与弱监督评估。

三个部分：

1. **参数白名单**：允许调优的参数只有下表所列，其余检索链路上的数值
   （冻结阈值、半衰期、BM25 与向量的 0.4/0.6 混合等）不在调优范围内。
   白名单外的名字在构造 :class:`TuningOverrides` 时即被拒绝。
2. **profile**：一组参数覆盖的命名快照，落 ``retrieval_profiles`` 表
   （由链尾 DDL 幂等建表，同 ``jargon`` 先例，不占迁移号）。生效机制是
   进程内覆盖表：检索链路的读取点先查覆盖、没有再用配置初值，
   ``default`` profile 的覆盖集为空，等于现状。覆盖表只随 profile 的
   应用、删除与启动恢复而整体替换。
3. **评估（留痕重放）**：从 ``memory_retrieval_trace`` 留痕取生产当轮的真实
   检索词（当前文本与会话印象）复现两次召回并集，与留痕 ``candidatePool``
   逐位对账，对不上的回合单独计数——那说明重放与生产仍有差异，其读数比
   nDCG 本身重要。重排走生产 ``rank_recalled_facts`` 的向量融合，再以
   nDCG@k 与召回条数打分；弱监督正例 = 首份生产提示词里的事实条目，且该
   回合产生了回复。WebUI 的评估接口响应取 :meth:`Round2Report.traced_as_dict`。
   评估不读写进程内覆盖表：待评估参数作为显式参数沿重放路径传到各打分点
   （召回打分、融合重排、候选池百分位、进提示词条数）。评估与生产检索会
   并发——WebUI 评估端点在线程池里执行评估，等待查询向量时也会让出事件
   循环——线上读取因此始终只看已生效 profile。

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
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.core.logging.logger import get_logger
from src.core.observe import events as trace

logger = get_logger(__name__)


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
#
# 评估不得写入此表，草稿参数一律经各读取函数的 ``overrides`` 参数显式传入。
# - 现象：评估期间临时替换此表时，同一时段到达的线上聊天回合按草稿参数召回。
# - 原因：WebUI 评估端点经 run_in_thread 在线程池里执行评估，评估也会在
#   await 查询向量时让出事件循环；线上召回在事件循环线程上同步读取此表，
#   两者共享同一个模块级对象。
# - 后果：草稿参数会改变线上提示词里的事实条目与条数，且不留任何生效记录。
# 替换只做整体重新绑定、不原地修改，读取方持有的引用始终是一致的快照。
_active_overrides: Dict[str, Any] = {}


def set_active_overrides(values: Mapping[str, Any]) -> None:
    """替换进程内生效的参数覆盖表。

    仅供 profile 的应用、删除与启动恢复使用；评估不调用本函数。

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


def _overrides_for(overrides: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    """选定一次读取所用的覆盖表：评估传入的草稿优先，省略时取已生效 profile。

    空表是合法草稿（即配置初值），只有 ``None`` 表示省略。
    """

    return _active_overrides if overrides is None else overrides


def tuned_value(
    name: str,
    config_default: Any,
    overrides: Optional[Mapping[str, Any]] = None,
) -> Any:
    """读取一个白名单参数的取值。

    :param name: 白名单参数名。
    :param config_default: 配置提供的初值；覆盖表未含该参数时使用。
    :param overrides: 评估重放的草稿覆盖表；省略时读已生效 profile。
    :return: 覆盖值或配置初值。
    副作用：无。
    """

    return _overrides_for(overrides).get(name, config_default)


def blend_score(
    relevance: float,
    retention_value: float,
    overrides: Optional[Mapping[str, Any]] = None,
) -> float:
    """按调优参数计算检索排序分。

    与 :func:`src.core.memory.decay.score` 的关系：两个权重取默认值
    （``bm25_weight=1.0``、``retention_weight_floor=0.35``）时与该函数
    逐字节等价；调优改的只是这两个系数，不改变乘法融合的形态。

    :param relevance: 词面相关度，通常已含向量融合。
    :param retention_value: 当前留存度。
    :param overrides: 评估重放的草稿覆盖表；省略时读已生效 profile。
    :return: 排序分数。
    副作用：无。
    """

    table = _overrides_for(overrides)
    weight = float(table.get('bm25_weight', 1.0))
    floor = float(table.get('retention_weight_floor', 0.35))
    relevance = max(0.0, relevance)
    return (relevance ** weight) * (floor + (1.0 - floor) * retention_value)


def ppr_alpha() -> float:
    """读取当前生效的 PPR 回迁概率；未覆盖时取现状常量。"""

    return float(_active_overrides.get('ppr_alpha', 0.85))


def ppr_hops(default_hops: int) -> int:
    """读取当前生效的 PPR 跳数；未覆盖时沿用调用方现状值。"""

    return int(_active_overrides.get('ppr_hops', default_hops))


def pool_percentile(overrides: Optional[Mapping[str, Any]] = None) -> float:
    """读取候选池分数百分位过滤阈值；``0.0`` 表示不过滤（现状）。

    :param overrides: 评估重放的草稿覆盖表；省略时读已生效 profile。
    """

    return float(_overrides_for(overrides).get('pool_score_percentile', 0.0))


def apply_pool_percentile(
    scores: Sequence[float],
    overrides: Optional[Mapping[str, Any]] = None,
) -> int:
    """计算百分位过滤后应保留的候选条数。

    :param scores: 池内分数（顺序与池一致）。
    :param overrides: 评估重放的草稿覆盖表；省略时读已生效 profile。
    :return: 保留条数；阈值为 ``0`` 时返回全长。阈值对分数最低的尾部生效，
        因此保留的总是分数最高的一段前缀（池已按分数降序排列）。
    副作用：无。
    """

    threshold = pool_percentile(overrides)
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


# ---------------------------------------------------------------- 留痕重放

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
    兜底）。没有留痕的回合不进样本。

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
    overrides: Optional[Mapping[str, Any]] = None,
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
    :param overrides: 评估的草稿覆盖表，用于召回打分与百分位截尾；省略时
        读已生效 profile。
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
            overrides=overrides,
        ):
            if fact.id not in seen:
                seen.add(fact.id)
                pool.append(fact)
    pool.sort(key=lambda fact: fact.score, reverse=True)
    kept = apply_pool_percentile([fact.score for fact in pool], overrides)
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
    """留痕重放评估的汇总结果。

    :ivar profile: 被评估的 profile 名；匿名参数集为 ``None``。
    :ivar params: 本次评估实际生效的覆盖表。
    :ivar k: nDCG 截断深度（进提示词条数上限）。
    :ivar traced: 留痕回合的汇总：样本数、按会话类型分组的读数与对账计数。
    :ivar embedding: 查询向量成本：调用次数、缓存命中与累计耗时毫秒。
    :ivar per_turn: 每回合一行明细。
    :ivar generated_at: 报告生成时刻。
    """

    profile: Optional[str]
    params: Dict[str, Any]
    k: int
    traced: Dict[str, Any]
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
            'embedding': self.embedding,
            'per_turn': self.per_turn,
            'generated_at': self.generated_at,
        }

    def traced_as_dict(self) -> Dict[str, Any]:
        """取整体列与可评分回合的明细，序列化为 WebUI 评估接口的响应。

        字段与 WebUI 的 ``TuningEvalReport`` 逐一对应，「现行 vs 草稿」对照按
        ``per_turn`` 的 ``turn_id`` 配对：

        - ``profile`` / ``params`` / ``k`` / ``generated_at``：同本报告。
        - ``sample_count``：可评分回合数（产生了回复且有事实进了提示词）。
          ``ndcg_mean``（nDCG@k 均值）、``displaced_positive_total``（正例落在
          前 k 之外的总条数）与 ``per_turn`` 只在这些回合上统计：无正例回合的
          nDCG 恒为 0，计入逐回合配对会使均值偏低。
        - ``replayed_count``：重放的留痕回合总数；``reconcile_mismatched``：
          其中候选池与留痕对不上的回合数。
        - ``recall_count_median`` / ``recall_count_mean``：重放下进提示词的
          条数，统计全部重放回合，与 :func:`_aggregate_stream_groups` 同口径。
        - ``per_turn`` 每行含 ``turn_id``、``stream_kind``、``positive_count``
          （正例条数）、``recall_count``（进提示词条数）、``ndcg`` 与
          ``displaced_positive``（该回合被挤掉的正例条数）。

        :return: API 响应字典。
        副作用：无。
        """

        overall = self.traced['groups']['all']
        return {
            'profile': self.profile,
            'params': self.params,
            'k': self.k,
            'sample_count': overall['scorable_count'],
            'replayed_count': overall['sample_count'],
            'reconcile_mismatched': self.traced['reconcile_mismatched'],
            'ndcg_mean': overall['ndcg_mean'],
            'recall_count_median': overall['recall_count_median'],
            'recall_count_mean': overall['recall_count_mean'],
            'displaced_positive_total': overall['displaced_positive_total'],
            'per_turn': [
                {
                    'turn_id': row['turn_id'],
                    'stream_kind': row['stream_kind'],
                    'positive_count': row['positive_count'],
                    'recall_count': row['recall_count'],
                    'ndcg': row['ndcg'],
                    'displaced_positive': row['displaced_positive'],
                }
                for row in self.per_turn
                if row['scorable']
            ],
            'generated_at': self.generated_at,
        }


async def evaluate_round2(
    store: Any,
    traced: Sequence[TraceSample],
    overrides: Mapping[str, Any],
    *,
    embed_query: Optional[Callable[[str], Awaitable[Optional[bytes]]]] = None,
    profile_name: Optional[str] = None,
    config_fact_limit: int = 6,
    private_in_group: bool = False,
) -> Round2Report:
    """按留痕检索词重放召回、并入向量融合重排，输出按会话类型分组的报告。

    每个回合用留痕检索词复现两次召回并集，并与 ``candidatePool`` 对账。融合
    重排直接调用生产的 ``rank_recalled_facts``，查询向量按生产口径取当前文本。

    查询向量按文本缓存：同一文本只调用一次 ``embed_query``，实际调用次数
    与累计耗时记入报告。``embed_query`` 为 ``None`` 时融合退化为词面排序，
    与生产在向量服务未装配时的行为一致。

    待评估参数显式传给重放召回、百分位截尾与融合重排，不经过进程内覆盖表：
    等待查询向量时事件循环会调度线上聊天，线上读取仍只看已生效 profile。
    重放只读，不强化、不回补、不写库。

    :param store: MemoryStore 实例。
    :param traced: :func:`build_trace_samples` 的产物。
    :param overrides: 本次评估的参数覆盖（先经白名单校验）；空表表示按配置
        初值评估，与当前生效 profile 无关。
    :param embed_query: 异步查询向量函数，签名与 ``VectorService.embed_query``
        一致；省略时融合退化。
    :param profile_name: 来源 profile 名；匿名评估为 ``None``。
    :param config_fact_limit: 配置提供的进提示词条数初值。
    :param private_in_group: ``conversation.private_facts_in_group`` 的当前值。
    :return: 汇总报告。
    :raises ValueError: 参数不在白名单。
    副作用：发出一条 ``retrieval_eval_round2_done`` 事件；对账期间可见性规则
        挡下条目时会发与生产同源的事件。
    """

    evaluated = validate_overrides(overrides)
    k = int(tuned_value('fact_recall_limit', config_fact_limit, evaluated))
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

    for sample in traced:
        present = approximate_present_persons(store, sample.stream_id, sample.at)
        replay = replay_union_recall(
            store, present, sample.current_text, sample.impression,
            k, sample.at,
            stream_kind=sample.stream_kind,
            private_in_group=private_in_group,
            overrides=evaluated,
        )
        reconcile = reconcile_trace_pool(replay, sample.candidate_pool)
        query_embedding = await query_vector(sample.current_text)
        ranked_ids = [
            fact.id for fact in store.rank_recalled_facts(
                replay, query_embedding, k, overrides=evaluated,
            )
        ]
        # 不可评分的回合照样重放与对账，只是不带正例、不进 nDCG 统计。
        positives = sample.prompt_fact_ids if sample.scorable else []
        per_turn.append({
            'turn_id': sample.turn_id,
            'stream_kind': sample.stream_kind,
            'scorable': sample.scorable,
            'reconcile': reconcile.as_dict(),
            'positive_count': len(positives),
            'candidate_count': len(replay),
            'recall_count': len(ranked_ids),
            'ranked_ids': ranked_ids,
            'ndcg': round(ndcg_at_k(ranked_ids, positives, k), 4),
            'displaced_positive': sum(
                1 for fact_id in positives if fact_id not in ranked_ids
            ),
        })

    statuses = [row['reconcile']['status'] for row in per_turn]
    report = Round2Report(
        profile=profile_name,
        params=evaluated,
        k=k,
        traced={
            'sample_count': len(per_turn),
            'groups': _aggregate_stream_groups(per_turn),
            'reconcile_matched': statuses.count('match'),
            'reconcile_mismatched': statuses.count('mismatch'),
        },
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
        traced=len(per_turn),
        reconcileMismatched=report.traced['reconcile_mismatched'],
        embeddingCalls=embed_calls,
    )
    logger.info(
        'retrieval_eval_round2_done',
        profile=profile_name or 'anonymous',
        traced=len(per_turn),
        reconcile_mismatched=report.traced['reconcile_mismatched'],
        embedding_calls=embed_calls,
    )
    return report
