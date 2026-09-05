"""人物画像派生层：把散落的事实与情节收敛成「Bot 对这个人的印象」。

画像是派生缓存，不是独立的事实来源。成立前提：画像内容都能追溯到具体的
fact 或 episode 记录；整张 ``person_profile`` 表删除不丢失任何信息，
可随时由本地证据重建。

v26 起画像分信任两档：

- **确凿档**（``confirmed`` 列）：facts 账本中仍有效的条目直接投影，
  逐条带可追溯的 fact id，按 slot / kind 组织，全程不经模型——
  模型产出永远进不了这一档；
- **印象档**（``summary`` 列）：模型从事实与情节收敛出来的理解，
  是唯一允许模型写入的一档。提示词约束（材料之外一个字都不写）
  仍然保留，但不再是唯一防线。

因此本模块：

- 只读 ``facts`` / ``episodes`` / ``persona_bond``，不把画像作为输入参与生成；
- 生成走独立的 ``memory`` 模型槽，在后台批量进行，不在回合关键路径上；
- 脏位由事实抽取那一侧置位（:func:`mark_dirty`），本模块只消费；
- 刷新先算证据指纹（参与生成的 fact / episode id 的稳定哈希），与库里的
  一致就只推进时间戳、清脏位，不再调用模型。

对外暴露 :func:`mark_dirty`（由事实抽取调用）、:func:`dirty_person_ids` /
:func:`render_evidence` / :func:`refresh_profiles`（后台刷新）与
:func:`profiles_for_injection`（提示词组装）。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import List, Optional, Sequence, Tuple

import json
import sqlite3

from src.core.runtime.clock import now as current_time
from src.core.llm_models.protocol import LlmProvider
from src.core.llm_models.snapshot import bind_render_params
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt, prompt_metadata

# 画像正文的字数上限。超长画像会挤占在场者事实的注入预算。
PROFILE_MAX_CHARS = 120
# 单次后台刷新最多处理多少人。刷新是模型调用，单轮处理全部待刷新项会让后台任务长期占用模型槽。
REFRESH_BATCH_LIMIT = 3
# 组装提示词时最多注入几份画像。群聊在场者可能十几个，全注入会淹没当前对话。
INJECT_LIMIT = 3
# 参与画像生成的证据上限：确凿档与交给模型的材料共用同一个事实集合，
# 超过上限的事实等下一轮强度排序进入，不进档也不进材料。
EVIDENCE_FACT_LIMIT = 20
EVIDENCE_EPISODE_LIMIT = 5


@dataclass(frozen=True)
class EvidenceFact:
    """一条参与画像生成的账本事实。

    :ivar fact_id: 事实行 ID；确凿档的可追溯锚点。
    :ivar label: 组织标签：单值槽位名（``slot``）优先，多值事实退化为类别（``kind``）。
    :ivar content: 事实正文。
    """

    fact_id: int
    label: str
    content: str


@dataclass(frozen=True)
class EvidenceEpisode:
    """一条参与画像生成的情节摘要。

    :ivar episode_id: 情节行 ID；证据指纹的组成部分。
    :ivar summary: 情节摘要正文。
    """

    episode_id: int
    summary: str


@dataclass(frozen=True)
class ProfileEvidence:
    """一次画像生成所依据的全部本地证据。

    :ivar person_id: 目标人物主键。
    :ivar facts: 该人仍有效（活跃且未被取代）的事实，按 slot / kind 组织排序。
    :ivar episodes: 该人参与过的情节摘要，按时间降序。
    """

    person_id: int
    facts: List[EvidenceFact]
    episodes: List[EvidenceEpisode]

    def is_empty(self) -> bool:
        """判断是否没有任何本地证据。

        :return: 事实与情节都为空时返回 ``True``。此时不生成画像：
            画像句子必须能追溯到具体来源。
        """
        return not self.facts and not self.episodes


@dataclass(frozen=True)
class InjectionProfile:
    """注入提示词的一份人物画像，确凿档与印象档分开呈现。

    :ivar person_id: 人物主键。
    :ivar confirmed: 确凿档条目，逐条可追溯 fact id；空元组表示暂无确凿内容。
    :ivar impression: 印象档正文；空串表示暂无印象。
    """

    person_id: int
    confirmed: Tuple[EvidenceFact, ...]
    impression: str


def mark_dirty(db: sqlite3.Connection, person_ids: Sequence[int], now: Optional[int] = None) -> int:
    """把这些人的画像标记为待刷新。

    由事实抽取在写入新事实后调用（脏位由事实抽取侧置位，本模块只消费）。
    行不存在时插入一条空画像并置脏，这样「从没有过画像的人」与「画像过期的人」
    在后台任务看来是同一种待办，不需要第二条代码路径。

    :param db: 当前库连接。
    :param person_ids: 需要刷新的人物主键；空列表时不做任何事。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: 实际置脏的人数。
    :raises sqlite3.Error: 写入或提交失败。
    副作用：写入 ``person_profile`` 并提交事务。
    """
    if not person_ids:
        return 0
    now = now if now is not None else current_time()
    for person_id in person_ids:
        db.execute(
            'INSERT INTO person_profile (person_id, summary, evidence_count, refreshed_at, dirty) '
            'VALUES (?, ?, 0, 0, 1) '
            'ON CONFLICT(person_id) DO UPDATE SET dirty = 1',
            (person_id, ''),
        )
    db.commit()
    return len(person_ids)


def dirty_person_ids(db: sqlite3.Connection, limit: int = REFRESH_BATCH_LIMIT) -> List[int]:
    """取出待刷新的人物主键。

    :param db: 当前库连接。
    :param limit: 单次最多返回多少人。
    :return: 待刷新人物主键列表，按最久没刷新的优先。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    rows = db.execute(
        'SELECT person_id FROM person_profile WHERE dirty = 1 '
        'ORDER BY refreshed_at ASC LIMIT ?',
        (limit,),
    ).fetchall()
    return [int(row[0]) for row in rows]


def render_evidence(db: sqlite3.Connection, person_id: int) -> ProfileEvidence:
    """读取一个人的画像证据。

    只取本地已有的事实与情节，不读 ``person_profile`` 自身：以旧版画像为输入
    会使内容逐轮漂移，偏离可追溯的证据。

    事实取「仍有效」集合——活跃且未被取代（``superseded_by IS NULL``），
    与检索、冲突检测的口径一致：已被取代的事实是账本明确失效的条目，
    不能再参与任何派生物的生成。

    :param db: 当前库连接。
    :param person_id: 目标人物主键。
    :return: 该人的事实与情节，均带可追溯的行 ID。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    facts = [
        EvidenceFact(
            fact_id=int(row[0]),
            label=str(row[1]) or str(row[2]),
            content=str(row[3]),
        )
        for row in db.execute(
            "SELECT id, slot, kind, content FROM facts "
            "WHERE person_id = ? AND active = 1 AND superseded_by IS NULL "
            "ORDER BY (slot = ''), slot, kind, strength DESC, id LIMIT ?",
            (person_id, EVIDENCE_FACT_LIMIT),
        )
    ]
    episodes = [
        EvidenceEpisode(episode_id=int(row[0]), summary=str(row[1]))
        for row in db.execute(
            'SELECT e.id, e.summary FROM episodes e '
            'JOIN messages m ON m.episode_id = e.id '
            'WHERE m.sender_person_id = ? AND e.summary <> \'\' '
            'GROUP BY e.id ORDER BY e.ended_at DESC LIMIT ?',
            (person_id, EVIDENCE_EPISODE_LIMIT),
        )
    ]
    return ProfileEvidence(person_id=person_id, facts=facts, episodes=episodes)


def evidence_fingerprint(evidence: ProfileEvidence) -> str:
    """计算参与生成的 fact / episode id 集合的稳定哈希。

    只哈希行 ID、不哈希正文：账本里正文从不原地改写（更正走 ``superseded_by``
    写新行），ID 集合不变即证据不变。指纹与生成方式无关，确凿档投影与模型材料
    共用同一份证据，也就共用一个指纹。

    :param evidence: 该人的事实与情节。
    :return: 十六进制摘要；证据为空时仍返回固定值（空集合的哈希）。
    副作用：无。
    """
    fact_ids = ','.join(str(item.fact_id) for item in
                        sorted(evidence.facts, key=lambda item: item.fact_id))
    episode_ids = ','.join(str(item.episode_id) for item in
                           sorted(evidence.episodes, key=lambda item: item.episode_id))
    return sha256(f'f[{fact_ids}]|e[{episode_ids}]'.encode('utf-8')).hexdigest()


def render_material(evidence: ProfileEvidence) -> str:
    """把证据组装成交给模型的材料正文。

    事实块明确标注「已在确凿档」：模型只负责印象档，重复确凿档内容既浪费
    字数预算，也让两档的界线在读者眼里重新糊掉。

    :param evidence: 该人的事实与情节。
    :return: 分段列出的材料文本；证据为空时返回空串。
    副作用：无。
    """
    blocks: List[str] = []
    if evidence.facts:
        blocks.append(
            '她的档案里已经记下的事（确凿，不要在印象里重复或改写）：\n'
            + '\n'.join(f'- {item.label}：{item.content}' for item in evidence.facts)
        )
    if evidence.episodes:
        blocks.append(
            '一起经历过的：\n' + '\n'.join(f'- {item.summary}' for item in evidence.episodes)
        )
    return '\n\n'.join(blocks)


async def generate_profile(
    provider: LlmProvider,
    evidence: ProfileEvidence,
    *,
    bot_name: str,
    temperature: float,
    max_tokens: Optional[int],
) -> Optional[str]:
    """请求模型把一个人的证据收敛成一段印象（只负责「印象」档）。

    :param provider: 提供流式文本输出的模型客户端，用 ``memory`` 任务槽。
    :param evidence: 该人的本地证据。
    :param bot_name: Bot 展示名，进系统提示词。
    :param temperature: 采样温度。
    :param max_tokens: 输出上限；``None`` 表示由 provider 决定。
    :return: 去掉首尾空白的印象正文。``None`` 只表示模型故障（本轮脏位保留、
        指纹不动，下轮整体重试）；模型按提示词契约输出空串（材料不足以形成
        印象）是正常的生成结果，返回 ``''``——两者必须分开，否则「没有印象」
        的人会被当作故障每轮重试，证据指纹永远省不下这次调用。
    副作用：发起一次流式模型请求并记录 ``llm_request`` 观测事件，不写库。
    """
    if evidence.is_empty():
        # 没有证据就不发请求：模型在空材料上只会生成无依据内容，且无法追溯到任何来源。
        return ''
    render_params = {'memory.profile': {'bot_name': bot_name, 'max_chars': str(PROFILE_MAX_CHARS)}}
    request_messages = [
        {
            'role': 'system',
            'content': get_prompt('memory.profile').render(
                bot_name=bot_name, max_chars=str(PROFILE_MAX_CHARS),
            ),
        },
        {'role': 'user', 'content': render_material(evidence)},
    ]
    raw = ''
    try:
        trace.emit(
            'llm_request',
            messages=request_messages,
            temperature=temperature,
            maxTokens=max_tokens,
            renderParams=render_params,
            **prompt_metadata('memory.profile', ('memory.profile',)),
        )
        bind_render_params(render_params)
        async for chunk in provider.stream(
            request_messages, temperature=temperature, max_tokens=max_tokens,
        ):
            if chunk.get('text'):
                raw += chunk['text']
    except Exception:
        # 画像是派生缓存：模型故障不该影响任何回合，本轮跳过、脏位保留，下次再来。
        return None
    # 超长直接截断而不是重试：上限是展示预算，不是正确性约束，多要一次往返不值得。
    return raw.strip().strip('"').strip('「」')[:PROFILE_MAX_CHARS]


def serialize_confirmed(facts: Sequence[EvidenceFact]) -> str:
    """把确凿档条目序列化为落库 JSON；每条保留 fact id 供回溯。

    确凿档是 facts 账本的直接投影：逐条取正文与标签，不增删、不改写，
    模型全程不参与。空集存空串而不是 ``'[]'``，与「暂无确凿内容」的
    读取口径一致。

    :param facts: 参与投影的账本事实。
    :return: JSON 文本；无事实时返回空串。
    副作用：无。
    """
    if not facts:
        return ''
    return json.dumps(
        [
            {'fact_id': item.fact_id, 'label': item.label, 'content': item.content}
            for item in facts
        ],
        ensure_ascii=False,
        separators=(',', ':'),
    )


def parse_confirmed(raw: str) -> Tuple[EvidenceFact, ...]:
    """把落库的确凿档 JSON 还原成条目序列。

    任何一步形状不符都按整档为空处理：缓存行损坏时宁可少注入，也不能把
    半个坏结构带进回合关键路径；重建由下一次刷新完成，不需要在这里修。

    :param raw: ``person_profile.confirmed`` 列文本。
    :return: 确凿档条目；空串或损坏内容返回空元组。
    副作用：无。
    """
    if not raw:
        return ()
    try:
        items = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(items, list):
        return ()
    entries: List[EvidenceFact] = []
    for item in items:
        if not isinstance(item, dict):
            return ()
        try:
            entries.append(EvidenceFact(
                fact_id=int(item['fact_id']),
                label=str(item['label']),
                content=str(item['content']),
            ))
        except (KeyError, TypeError, ValueError):
            return ()
    return tuple(entries)


def write_profile(
    db: sqlite3.Connection,
    person_id: int,
    impression: str,
    confirmed: str,
    evidence_count: int,
    fingerprint: str,
    now: int,
) -> None:
    """落库一份刷新后的画像并清除脏位。

    确凿档与印象档连同证据指纹一次性写入：两档必须对应同一份证据，
    只落一半会让指纹失去意义（指纹命中时另一方却不一定是最新的）。

    :param db: 当前库连接。
    :param person_id: 目标人物主键。
    :param impression: 印象档正文（``summary`` 列）；允许为空串，表示证据
        不足以形成印象。
    :param confirmed: 确凿档 JSON（:func:`serialize_confirmed` 的产物）。
    :param evidence_count: 本次用到的事实与情节条数，便于核对画像的证据条数。
    :param fingerprint: 本次证据的指纹（:func:`evidence_fingerprint` 的产物）。
    :param now: 当前毫秒时间戳。
    :raises sqlite3.Error: 写入或提交失败。
    副作用：写入 ``person_profile`` 并提交事务。
    """
    db.execute(
        'INSERT INTO person_profile '
        '(person_id, summary, confirmed, evidence_fingerprint, evidence_count, refreshed_at, dirty) '
        'VALUES (?, ?, ?, ?, ?, ?, 0) '
        'ON CONFLICT(person_id) DO UPDATE SET '
        'summary = excluded.summary, confirmed = excluded.confirmed, '
        'evidence_fingerprint = excluded.evidence_fingerprint, '
        'evidence_count = excluded.evidence_count, '
        'refreshed_at = excluded.refreshed_at, dirty = 0',
        (person_id, impression, confirmed, fingerprint, evidence_count, now),
    )
    db.commit()


def _stored_fingerprint(db: sqlite3.Connection, person_id: int) -> str:
    """读取该人上一轮刷新存下的证据指纹；没有行或没刷过（迁移存量）返回空串。

    空串永远不等于真实指纹（SHA-256 十六进制），存量行因此必然先完整刷新一次。
    """

    row = db.execute(
        'SELECT evidence_fingerprint FROM person_profile WHERE person_id = ?',
        (person_id,),
    ).fetchone()
    return str(row[0]) if row else ''


def _touch_profile(db: sqlite3.Connection, person_id: int, now: int) -> None:
    """指纹命中时的轻量落库：只推进时间戳、清脏位，两档正文与指纹不动。"""

    db.execute(
        'UPDATE person_profile SET refreshed_at = ?, dirty = 0 WHERE person_id = ?',
        (now, person_id),
    )
    db.commit()


async def refresh_profiles(
    db: sqlite3.Connection,
    provider: LlmProvider,
    *,
    bot_name: str,
    temperature: float,
    max_tokens: Optional[int],
    limit: int = REFRESH_BATCH_LIMIT,
    now: Optional[int] = None,
) -> int:
    """批量刷新待更新的人物画像。

    不在回合关键路径上调用：与摘要、事实抽取同一纪律，由回合收尾派生的
    后台任务驱动，任何失败都不阻塞回复。

    每个人先算证据指纹，与库里的比对：

    - 指纹一致：证据集没变，确凿档（账本的确定性投影）与印象材料都不会变，
      只推进 ``refreshed_at``、清脏位，不调用模型，并发
      ``profile_refresh_skipped`` 事件——否则「画像怎么没更新」无从排查；
    - 指纹不一致：确凿档由 facts 账本直接投影（不调模型），印象档走模型，
      两档与新指纹一起整行写入。

    迁移进来的存量行（指纹为空串）与普通过期行走同一条路径：本地证据重算后
    整条覆盖，不做新旧合并。存量 ``summary`` 来自旧版自由文本，没有本地证据
    支撑，合并逻辑的复杂度高于其价值。

    :param db: 当前库连接。
    :param provider: ``memory`` 任务槽的模型客户端。
    :param bot_name: Bot 展示名。
    :param temperature: 采样温度。
    :param max_tokens: 输出上限。
    :param limit: 本轮最多处理多少人。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: 脏位被清除的人数（含指纹命中跳过模型调用的人）。
    :raises sqlite3.Error: 读写失败。
    副作用：可能发起多次模型请求并写入 ``person_profile``。
    """
    now = now if now is not None else current_time()
    refreshed = 0
    skipped = 0
    for person_id in dirty_person_ids(db, limit):
        evidence = render_evidence(db, person_id)
        fingerprint = evidence_fingerprint(evidence)
        if fingerprint == _stored_fingerprint(db, person_id):
            # 证据集没变：确凿档是账本的确定性投影、印象材料也没变，
            # 只推进时间戳并清脏位，不再调用模型。
            _touch_profile(db, person_id, now)
            skipped += 1
            trace.emit('profile_refresh_skipped', personId=person_id, reason='evidence_unchanged')
            continue
        impression = await generate_profile(
            provider, evidence,
            bot_name=bot_name, temperature=temperature, max_tokens=max_tokens,
        )
        if impression is None:
            # 模型故障：保留脏位与旧指纹，下一轮整体重试。确凿档与印象档必须
            # 对应同一份证据指纹，不能只落一半。
            continue
        write_profile(
            db, person_id, impression,
            serialize_confirmed(evidence.facts),
            len(evidence.facts) + len(evidence.episodes),
            fingerprint, now,
        )
        refreshed += 1
    if refreshed or skipped:
        trace.emit('profile_refreshed', count=refreshed, skipped=skipped)
    return refreshed + skipped


def profiles_for_injection(
    db: sqlite3.Connection,
    person_ids: Sequence[int],
    limit: int = INJECT_LIMIT,
    *,
    skip_dirty: bool = False,
) -> List[InjectionProfile]:
    """取在场者中最该注入的几份画像。

    在场者多于上限时按 ``persona_bond.intimacy`` 降序取前几个：亲密度更高者的
    画像优先进入本轮上下文。确凿档与印象档以结构化条目返回，分两档呈现的
    措辞由提示词组装侧负责。

    :param db: 当前库连接。
    :param person_ids: 本轮在场者的人物主键。
    :param limit: 最多返回几份。
    :param skip_dirty: 为真时跳过带脏位的画像：纠错之后旧快照可能还引用着
        被纠正的事实，宁可在后台刷新前不注入，也不复用过期内容。
    :return: 画像条目列表；确凿档与印象档都为空的人不算数，直接跳过。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    if not person_ids:
        return []
    marks = ','.join('?' for _ in person_ids)
    rows = db.execute(
        f'SELECT p.person_id, p.confirmed, p.summary FROM person_profile p '
        f'LEFT JOIN persona_bond b ON b.person_id = p.person_id '
        f'WHERE p.person_id IN ({marks}) '
        f"AND (p.summary <> '' OR p.confirmed <> '') "
        f'AND (? = 0 OR p.dirty = 0) '
        f'ORDER BY COALESCE(b.intimacy, 0) DESC LIMIT ?',
        (*person_ids, int(skip_dirty), limit),
    ).fetchall()
    return [
        InjectionProfile(
            person_id=int(row[0]),
            confirmed=parse_confirmed(str(row[1])),
            impression=str(row[2]),
        )
        for row in rows
    ]
