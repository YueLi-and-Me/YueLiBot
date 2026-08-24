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
