"""人物画像派生层（W5）：把散落的事实与情节收敛成「她对这个人的印象」。

**画像是派生缓存，不是第三份真相。** 这一条是本模块存在的前提：总纲第八节原本
写着「PersonInfo 宽表不做」，理由是人物印象已由 ``facts`` 与 ``persona_bond``
分别承担，再加一张就是三份重复真相。2026-08-23 的裁决翻转了那一条，但保留了
原来的理由——解法必须满足：画像里的每一句都能追溯到某条 fact 或 episode，
整张 ``person_profile`` 删掉不丢任何信息，重建即可。

因此本模块：

- 只**读** ``facts`` / ``episodes`` / ``persona_bond``，从不把画像当作输入再喂给自己；
- 生成走独立的 ``memory`` 模型槽，在后台批量进行，**绝不在回合关键路径上**；
- 脏位由事实抽取那一侧置位（:func:`mark_dirty`），本模块只消费。

对外暴露 :func:`mark_dirty`（W1 调用）、:func:`dirty_person_ids` /
:func:`render_evidence` / :func:`refresh_profiles`（后台刷新）与
:func:`profiles_for_injection`（提示词组装）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

import sqlite3

from src.core.common.clock import now as current_time
from src.core.llm_models.protocol import LlmProvider
from src.core.llm_models.snapshot import bind_render_params
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt, prompt_metadata

# 画像正文的字数上限。**本模块唯一的常量**：它能单独观察效果（看注入了多长），
# 不与任何其它取值互相牵制。超过这个长度的画像会挤占在场者事实的注入预算。
PROFILE_MAX_CHARS = 120
# 单次后台刷新最多处理多少人。刷新是模型调用，一轮包圆会让后台任务长期占着模型槽。
REFRESH_BATCH_LIMIT = 3
# 组装提示词时最多注入几份画像。群聊在场者可能十几个，全注入会淹没当前对话。
INJECT_LIMIT = 3
# 参与画像生成的证据上限：事实按 retention 降序、情节按时间降序。
EVIDENCE_FACT_LIMIT = 20
EVIDENCE_EPISODE_LIMIT = 5


@dataclass(frozen=True)
class ProfileEvidence:
    """一次画像生成所依据的全部本地证据。

    :ivar person_id: 目标人物主键。
    :ivar facts: 该人的事实正文，按 retention 降序。
    :ivar episodes: 该人参与过的情节摘要，按时间降序。
    """

    person_id: int
    facts: List[str]
    episodes: List[str]

    def is_empty(self) -> bool:
        """判断是否没有任何本地证据。

        :return: 事实与情节都为空时返回 ``True``——此时不该生成画像，
            凭空写出来的句子无法追溯到任何来源。
        """
        return not self.facts and not self.episodes


def mark_dirty(db: sqlite3.Connection, person_ids: Sequence[int], now: Optional[int] = None) -> int:
    """把这些人的画像标记为待刷新。

    由事实抽取在写入新事实后调用（规格：脏位由 W1 侧置位，本模块只消费）。
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

    只取本地已有的事实与情节。**不读 ``person_profile`` 自身**——把上一版画像
    当输入会让内容逐轮漂移，几轮之后就再也追溯不到具体证据了。

    :param db: 当前库连接。
    :param person_id: 目标人物主键。
    :return: 该人的事实与情节正文。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    facts = [
        str(row[0])
        for row in db.execute(
            'SELECT content FROM facts WHERE person_id = ? AND active = 1 '
            'ORDER BY strength DESC LIMIT ?',
            (person_id, EVIDENCE_FACT_LIMIT),
        )
    ]
    episodes = [
        str(row[0])
        for row in db.execute(
            'SELECT e.summary FROM episodes e '
            'JOIN messages m ON m.episode_id = e.id '
            'WHERE m.sender_person_id = ? AND e.summary <> \'\' '
            'GROUP BY e.id ORDER BY e.ended_at DESC LIMIT ?',
            (person_id, EVIDENCE_EPISODE_LIMIT),
        )
    ]
    return ProfileEvidence(person_id=person_id, facts=facts, episodes=episodes)


def render_material(evidence: ProfileEvidence) -> str:
    """把证据组装成交给模型的材料正文。

    :param evidence: 该人的事实与情节。
    :return: 分段列出的材料文本；证据为空时返回空串。
    副作用：无。
    """
    blocks: List[str] = []
    if evidence.facts:
        blocks.append('她记住的事：\n' + '\n'.join(f'- {item}' for item in evidence.facts))
    if evidence.episodes:
        blocks.append('一起经历过的：\n' + '\n'.join(f'- {item}' for item in evidence.episodes))
    return '\n\n'.join(blocks)


async def generate_profile(
    provider: LlmProvider,
    evidence: ProfileEvidence,
    *,
    bot_name: str,
    temperature: float,
    max_tokens: Optional[int],
) -> Optional[str]:
    """请求模型把一个人的证据收敛成一段印象。

    :param provider: 提供流式文本输出的模型客户端，用 ``memory`` 任务槽。
    :param evidence: 该人的本地证据。
    :param bot_name: Bot 展示名，进系统提示词。
    :param temperature: 采样温度。
    :param max_tokens: 输出上限；``None`` 表示由 provider 决定。
    :return: 去掉首尾空白的画像正文；证据为空、模型失败或输出为空时返回 ``None``。
    副作用：发起一次流式模型请求并记录 ``llm_request`` 观测事件，不写库。
    """
    if evidence.is_empty():
        # 没有证据就不发请求：模型在空材料上只会编，而编出来的句子追溯不到任何来源。
        return None
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
    text = raw.strip().strip('"').strip('「」')
    if not text:
        return None
    # 超长直接截断而不是重试：上限是展示预算，不是正确性约束，多要一次往返不值得。
    return text[:PROFILE_MAX_CHARS]


def write_profile(
    db: sqlite3.Connection,
    person_id: int,
    summary: str,
    evidence_count: int,
    now: int,
) -> None:
    """落库一份刷新后的画像并清除脏位。

    :param db: 当前库连接。
    :param person_id: 目标人物主键。
    :param summary: 画像正文；允许为空串，表示「证据不足，暂时没有印象」。
    :param evidence_count: 本次用到的事实与情节条数，便于事后核对画像的依据厚度。
    :param now: 当前毫秒时间戳。
    :raises sqlite3.Error: 写入或提交失败。
    副作用：写入 ``person_profile`` 并提交事务。
    """
    db.execute(
        'INSERT INTO person_profile (person_id, summary, evidence_count, refreshed_at, dirty) '
        'VALUES (?, ?, ?, ?, 0) '
        'ON CONFLICT(person_id) DO UPDATE SET '
        'summary = excluded.summary, evidence_count = excluded.evidence_count, '
        'refreshed_at = excluded.refreshed_at, dirty = 0',
        (person_id, summary, evidence_count, now),
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

    **绝不在回合关键路径上调用**：与摘要、事实抽取同一条纪律，由回合收尾派生的
    后台任务驱动，任何失败都不阻塞回复。

    迁移进来的种子行（``evidence_count = 0``）与普通过期行走**同一条路径**：
    本地证据重算后整条覆盖，不做新旧合并——那 12 条种子来自 2025-07 的旧库，
    没有任何本地证据支撑，合并逻辑的复杂度远大于它的价值。

    :param db: 当前库连接。
    :param provider: ``memory`` 任务槽的模型客户端。
    :param bot_name: Bot 展示名。
    :param temperature: 采样温度。
    :param max_tokens: 输出上限。
    :param limit: 本轮最多处理多少人。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: 实际刷新（脏位被清除）的人数。
    :raises sqlite3.Error: 读写失败。
    副作用：可能发起多次模型请求并写入 ``person_profile``。
    """
    now = now if now is not None else current_time()
    refreshed = 0
    for person_id in dirty_person_ids(db, limit):
        evidence = render_evidence(db, person_id)
        summary = await generate_profile(
            provider, evidence,
            bot_name=bot_name, temperature=temperature, max_tokens=max_tokens,
        )
        if summary is None and not evidence.is_empty():
            # 有证据却没拿到结果，说明是模型故障：保留脏位，下一轮重试。
            continue
        write_profile(db, person_id, summary or '', len(evidence.facts) + len(evidence.episodes), now)
        refreshed += 1
    if refreshed:
        trace.emit('profile_refreshed', count=refreshed)
    return refreshed


def profiles_for_injection(
    db: sqlite3.Connection,
    person_ids: Sequence[int],
    limit: int = INJECT_LIMIT,
) -> List[tuple[int, str]]:
    """取在场者中最该注入的几份画像。

    在场者多于上限时按 ``persona_bond.intimacy`` 降序取前几个——她对谁印象更深，
    谁的画像就更该出现在这一轮的上下文里。

    :param db: 当前库连接。
    :param person_ids: 本轮在场者的人物主键。
    :param limit: 最多返回几份。
    :return: ``(person_id, summary)`` 列表；``summary`` 为空的人不算数，直接跳过。
    :raises sqlite3.Error: 查询失败。
    副作用：只读。
    """
    if not person_ids:
        return []
    marks = ','.join('?' for _ in person_ids)
    rows = db.execute(
        f'SELECT p.person_id, p.summary FROM person_profile p '
        f'LEFT JOIN persona_bond b ON b.person_id = p.person_id '
        f'WHERE p.person_id IN ({marks}) AND p.summary <> \'\' '
        f'ORDER BY COALESCE(b.intimacy, 0) DESC LIMIT ?',
        (*person_ids, limit),
    ).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]
