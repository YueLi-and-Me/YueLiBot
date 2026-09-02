"""实现三层记忆的 SQLite 读写、事务更新和相关内容召回。

调用方注入已打开的 ``sqlite3.Connection``，本模块不决定数据库文件位置；因此
生产环境可由统一连接管理器控制迁移和生命周期，测试可使用 ``:memory:`` 隔离。
``messages`` 保存近期对话，``episodes`` 保存摘要，``facts`` 保存带强度和遗忘
曲线的结构化事实。衰减、相似度和分词算法分别由同目录模块提供。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import json
import sqlite3

from .decay import (
    FREEZE, DecayState, evaluate, freeze_due_at, half_life_for,
    reinforce, relevance_from_bm25, retention, retention_weight, score,
)
from .similarity import exact_key, is_same_fact
from .scope import SCOPE_ALL, fact_visible_in_stream
from .tokenize import index_tokens, match_query, words

from src.core.common.clock import now as current_time
from src.core.common.db.schema import DDL, SCHEMA_VERSION, SEED
from src.core.observe import events as trace


_PENDING_PROMISES_KEY = 'pending_promises'

# 历史消息检索的查询词上限。LIKE 条件按 OR 拼接，词数越多预筛越接近全表扫描，
# 而排在后面的低频词对命中排序几乎没有贡献。
_MESSAGE_QUERY_TERMS = 6
# 历史消息检索的预筛行数硬上界。没有 FTS 索引，靠这个上界把最坏情况钉死；
# 超出上界时优先保留时间靠后的消息，与「最近提过的那次」这一检索意图一致。
_MESSAGE_SCAN_LIMIT = 200

# 助手动作伪消息既要进入普通历史供 Bot 回看，又不能被后台模型当成亲口说过的话。
# 格式化与识别共用这些片段，避免写入方和消费方各维护一套字符串口径。
_ASSISTANT_POKE_ACTION_PREFIX = '[戳了戳 '
_ASSISTANT_REACTION_ACTION_PREFIX = '[给消息 '
_ASSISTANT_REACTION_ACTION_SEPARATOR = ' 贴了个「'
_ASSISTANT_REACTION_ACTION_SUFFIX = '」]'


def format_assistant_poke_action(target_name: str) -> str:
    """格式化一条成功戳一戳的助手动作历史。"""

    return f'{_ASSISTANT_POKE_ACTION_PREFIX}{target_name}]'


def format_assistant_reaction_action(target_message_id: int, reaction: str) -> str:
    """格式化一条成功贴表情回应的助手动作历史。"""

    return (
        f'{_ASSISTANT_REACTION_ACTION_PREFIX}{target_message_id}'
        f'{_ASSISTANT_REACTION_ACTION_SEPARATOR}{reaction}'
        f'{_ASSISTANT_REACTION_ACTION_SUFFIX}'
    )


def is_assistant_action_message(content: str | None) -> bool:
    """判断助手消息是否为本模块定义的动作历史，而非真实发言。

    只识别两个写入函数产生的完整结构；普通的方括号发言、空括号和结构不完整的
    文本都返回 ``False``，避免把 Bot 正常说出的 ``[...]`` 内容排除在学习与抽取之外。
    """

    text = (content or '').strip()
    if text.startswith(_ASSISTANT_POKE_ACTION_PREFIX) and text.endswith(']'):
        target_name = text[len(_ASSISTANT_POKE_ACTION_PREFIX):-1]
        return bool(target_name.strip())
    if not (
        text.startswith(_ASSISTANT_REACTION_ACTION_PREFIX)
        and text.endswith(_ASSISTANT_REACTION_ACTION_SUFFIX)
    ):
        return False
    body = text[
        len(_ASSISTANT_REACTION_ACTION_PREFIX):-len(_ASSISTANT_REACTION_ACTION_SUFFIX)
    ]
    target_message_id, separator, reaction = body.partition(
        _ASSISTANT_REACTION_ACTION_SEPARATOR,
    )
    return bool(separator and target_message_id.isdecimal() and reaction.strip())


@dataclass
class StoredMessage:
    """表示从 L1 工作记忆读取的一条消息。

    :ivar role: 消息角色，通常为 `user` 或 `assistant`。
    :ivar content: 消息正文。
    :ivar created_at: 创建时间的 Unix 毫秒时间戳。
    :ivar sender_person_id: 发送者人物 ID；助手消息可为 `None`。
    :ivar message_id: 消息表主键，用于按批次重建交错落库后的逻辑顺序。
    """

    role: str   # 'user' | 'assistant'
    content: str
    created_at: int
    sender_person_id: int | None
    message_id: int


@dataclass
class FactInput:
    """表示待写入 L3 事实的内容、分类与账本字段。

    :ivar content: 事实正文。
    :ivar kind: 事实类型，默认值为 `未分类`。
    :ivar slot: 单值槽位名（居住地、职业、生日……）；空串表示多值事实，
        多值事实之间永不判冲突。
    :ivar supersedes: 显式声明取代的既有事实 ID；``0`` 表示不取代。
    """

    content: str
    kind: str = '未分类'
    slot: str = ''
    supersedes: int = 0


@dataclass
class FactWrite:
    """表示 ``MemoryStore.add_fact`` 的一次写入结果。

    :ivar fact_id: 新建或被强化的事实 ID；正文归一化后为空、未写入时为 ``0``。
    :ivar created: 本次是否新建了行；``False`` 表示命中相似事实、只做了强化。
    :ivar conflict_with: 同一 ``(person_id, slot)`` 下仍活跃的其他事实 ID；
        空列表表示没有冲突。冲突只上报不解决：两条都保留，由提示词并排呈现。
    :ivar superseded: 本次被显式取代（回填 ``superseded_by``）的旧行 ID；
        ``0`` 表示没有发生取代。
    """

    fact_id: int
    created: bool = False
    conflict_with: list[int] = field(default_factory=list)
    superseded: int = 0


@dataclass
class RecalledFact:
    """表示召回结果中的事实及其排序指标。

    :ivar id: facts 表主键。
    :ivar kind: 事实类型。
    :ivar content: 事实正文。
    :ivar retention: 当前留存度。
    :ivar score: 当前召回排序分数。
    """

    id: int
    kind: str
    content: str
    retention: float
    score: float
    lexical_relevance: float = field(default=0.0, repr=False, compare=False)
    embedding: bytes | None = field(default=None, repr=False, compare=False)
    half_life_hours: float = field(default=0.0, repr=False, compare=False)


@dataclass
class ScopedFact(RecalledFact):
    """表示跨人物召回时附带归属人物的事实。

    :ivar person_id: 事实所属人物 ID；跨人物召回必须带上它，否则渲染观察时
        无法说明「这是关于谁的」，多人群聊里等同于把事实说成无主信息。
    """

    person_id: int = 0


@dataclass
class StoredFact(RecalledFact):
    """表示包含冻结状态和下次评估时间的完整事实记录。

    :ivar due_at: 下次衰减评估时间戳，默认值为 0。
    :ivar frozen: 是否已冻结，默认值为 `False`。
    """

    due_at: int = 0
    frozen: bool = False


# 归档占位情节的 kind。摘要模型对某一批消息确定性失败（例如被内容策略拒绝）时，
# 用它占住归档位让待摘要队列继续前进，否则同一批会被无限重投。
#
# 占位情节不写 cues，因此 recall_episodes 的 FTS 路径检索不到它；只有
# recent_episodes 按时间倒序取，必须显式排除，否则占位文本会当作真实情节
# 进入工作记忆上下文。all_episodes 不排除，让它在 WebUI 里可见。
UNSUMMARIZED_KIND = 'unsummarized'


@dataclass
class RecalledEpisode:
    """表示情节记忆召回结果。

    :ivar id: episodes 表主键。
    :ivar summary: 情节摘要。
    :ivar kind: 情节类型。
    :ivar ended_at: 情节结束时间戳。
    :ivar score: 召回排序分数。
    """

    id: int
    summary: str
    kind: str
    ended_at: int
    score: float


@dataclass
class EpisodeInput:
    """表示待写入 L2 情节记忆的摘要和关联消息。

    :ivar summary: 情节摘要正文。
    :ivar cues: 用于 FTS5 召回的线索列表。
    :ivar started_at: 情节开始时间戳。
    :ivar ended_at: 情节结束时间戳。
    :ivar message_ids: 要归档到该情节的消息 ID 列表。
    :ivar kind: 情节类型，默认值为 `conversation`。
    """

    summary: str
    cues: list[str]
    started_at: int
    ended_at: int
    message_ids: list[int]
    kind: str = 'conversation'


class MemoryStore:
    """封装消息、情节、事实和待办约定的 SQLite 读写。

    初始化时执行当前 DDL/SEED 并写入 schema 版本；数据库迁移应在构造此类之前由
    `common.db.migrations.manager` 完成。
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        """绑定已打开的 SQLite 连接并确保当前表结构和种子存在。

        :param db: `check_same_thread=False` 的 SQLite 连接。
        副作用：执行 DDL、SEED、schema_version 写入并提交事务。
        :raises sqlite3.Error: 建表、种子写入或提交失败。
        """
        self._db = db
        db.executescript(DDL)
        db.executescript(SEED)
        db.execute(
            'INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)',
            ('schema_version', str(SCHEMA_VERSION))
        )
        db.commit()

    # ------------------------------------------------------------------ 属性
    def first_seen_at(self, person_id: int) -> int:
        """读取人物首次出现的 Unix 毫秒时间戳。

        :param person_id: persons 表主键。
        :return: 人物的 `first_seen_at`。
        :raises RuntimeError: 人物不存在。
        :raises sqlite3.Error: 查询失败。
        副作用：只读 persons 表。
        """
        row = self._db.execute(
            "SELECT first_seen_at FROM persons WHERE id = ?", (person_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"person {person_id} 不存在，必须先经 StreamRegistry 解析")
        return row[0]

    # ------------------------------------------------------------------ L1 工作记忆
    def append_message(
        self,
        stream_id: int,
        sender_person_id: int | None,
        role: str,
        content: str,
        now: int | None = None,
        external_message_id: str | None = None,
    ) -> int:
        """向指定 stream 的 L1 工作记忆追加一条消息。

        :param stream_id: 目标 stream ID。
        :param sender_person_id: 发送者人物 ID；`role='user'` 时必须非空。
        :param role: 消息角色。
        :param content: 消息正文。
        :param now: 可选创建时间戳；省略时读取当前毫秒时钟。
        :param external_message_id: 可选的平台原生消息编号；出站引用回复据此把
            内部消息 ID 还原成平台编号。桌面等无编号通道传 ``None``。
        :return: 新消息的数据库 ID。
        :raises ValueError: 用户消息缺少发送者人物 ID。
        :raises sqlite3.Error: 插入或提交失败。
        副作用：写入 messages 表并提交事务。
        """
        if role == 'user' and sender_person_id is None:
            raise ValueError('user 消息必须携带 sender_person_id')
        now = now if now is not None else current_time()
        cur = self._db.execute(
            '''INSERT INTO messages
                   (stream_id, sender_person_id, role, content, created_at, external_message_id)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (stream_id, sender_person_id, role, content, now, external_message_id)
        )
        self._db.commit()
        return cur.lastrowid or 0

    def external_message_id(self, stream_id: int, id: int) -> str | None:
        """读取一条消息的平台原生编号。

        :param stream_id: 消息所属 stream ID，防止跨 stream 读到同号消息。
        :param id: 消息主键。
        :return: 平台消息编号；消息不存在、不属于该 stream 或入库时未带编号时返回 ``None``。
        :raises sqlite3.Error: 查询失败。
        """
        row = self._db.execute(
            'SELECT external_message_id FROM messages WHERE id = ? AND stream_id = ?',
            (id, stream_id)
        ).fetchone()
        if row is None:
            return None
        return row['external_message_id']

    def update_message_content(self, stream_id: int, id: int, content: str) -> int:
        """用后台补齐后的正文替换一条已落库消息的内容。

        :param stream_id: 消息所属 stream ID。
        :param id: 消息主键。
        :param content: 替换后的非空消息正文。
        :return: 受影响的行数，正常情况下为 ``1``。
        :raises sqlite3.Error: 更新或提交失败。
        副作用：只更新匹配 stream 和主键的消息内容并提交事务。
        """
        cur = self._db.execute(
            'UPDATE messages SET content = ? WHERE id = ? AND stream_id = ?',
            (content, id, stream_id),
        )
        self._db.commit()
        return cur.rowcount

    def delete_message(self, stream_id: int, id: int) -> None:
        """删除指定 stream 中的一条消息。

        :param stream_id: 消息所属 stream ID。
        :param id: 消息主键。
        :return: 无返回值；目标不存在时不报错。
        :raises sqlite3.Error: 删除或提交失败。
        副作用：从 messages 表删除匹配行并提交事务。
        """
        self._db.execute('DELETE FROM messages WHERE id = ? AND stream_id = ?', (id, stream_id))
        self._db.commit()

    def working_memory(
        self,
        stream_id: int,
        limit: int = 40,
        user_message_id_watermark: int | None = None,
    ) -> list[StoredMessage]:
        """读取指定 stream 尚未归档的最近工作记忆。

        :param stream_id: 目标 stream ID。
        :param limit: 最多返回的消息数，默认值为 40。
        :param user_message_id_watermark: 可选的本批末条用户消息 ID；水位后的用户消息不进入历史。
        :return: 按时间正序排列的 `StoredMessage` 列表。
        :raises sqlite3.Error: 查询失败。
        副作用：只读 messages 表。
        """
        if user_message_id_watermark is None:
            rows = self._db.execute(
                '''SELECT id, role, content, created_at, sender_person_id FROM messages
                   WHERE stream_id = ? AND episode_id IS NULL ORDER BY id DESC LIMIT ?''',
                (stream_id, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                '''SELECT id, role, content, created_at, sender_person_id FROM messages
                   WHERE stream_id = ? AND episode_id IS NULL
                   AND NOT (role = 'user' AND id > ?) ORDER BY id DESC LIMIT ?''',
                (stream_id, user_message_id_watermark, limit),
            ).fetchall()
        return [
            StoredMessage(
                role=r[1],
                content=r[2],
                created_at=r[3],
                sender_person_id=r[4],
                message_id=r[0],
            )
            for r in reversed(rows)
        ]

    def has_user_messages_after(self, stream_id: int, id: int) -> bool:
        """判断指定消息之后该 stream 是否还有别人发的消息。

        出站引用据此判断「这条回复落地时是否已经被别的发言冲开」：目标之后还有
        别人说话，说明旁观者已经看不出 Bot 在回哪一条，需要挂引用点明。只看
        ``role='user'``：Bot 自己这一轮的回复正文在投递前就已落库，算进来会让判据
        恒真。

        :param stream_id: 目标 stream ID。
        :param id: 作为分界的消息主键。
        :return: 存在主键更大的同 stream 用户消息时返回 ``True``。
        :raises sqlite3.Error: 查询失败。
        """
        row = self._db.execute(
            "SELECT 1 FROM messages WHERE stream_id = ? AND id > ? AND role = 'user' LIMIT 1",
            (stream_id, id)
        ).fetchone()
        return row is not None

    def last_message_at(self, stream_id: int) -> int | None:
        """返回指定 stream 最近一条消息的时间戳。

        :param stream_id: 目标 stream ID。
        :return: 最大 `created_at`；没有消息时返回 `None`。
        副作用：只读 messages 表。
        """
        row = self._db.execute(
            'SELECT MAX(created_at) FROM messages WHERE stream_id = ?', (stream_id,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def last_assistant_reply_at(self, stream_id: int) -> int | None:
        """返回指定 stream 最近一条助手回复的落库时间。

        natural_reply_window 必须按 Bot 实际发言时间收窄，不能复用十分钟
        回复计数；调用方用该时间戳与当前时刻的差值判断自然跟进窗口。

        :param stream_id: 目标 stream ID。
        :return: 最近助手消息的最大 ``created_at``；没有助手消息时返回 ``None``。
        :raises sqlite3.Error: 查询失败。
        副作用：只读 messages 表。
        """
        row = self._db.execute(
            '''SELECT MAX(created_at) FROM messages
               WHERE stream_id = ? AND role = 'assistant' ''',
            (stream_id,),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def last_user_message_at(self, stream_id: int) -> int | None:
        """返回指定 stream 最近一条用户消息的落库时间。

        与 ``last_assistant_reply_at`` 配对使用，调用方比较两者先后即可判断
        「Bot 说完之后对方是否回过话」，不必再拉一遍完整历史。

        :param stream_id: 目标 stream ID。
        :return: 最近用户消息的最大 ``created_at``；没有用户消息时返回 ``None``。
        :raises sqlite3.Error: 查询失败。
        副作用：只读 messages 表。
        """
        row = self._db.execute(
            '''SELECT MAX(created_at) FROM messages
               WHERE stream_id = ? AND role = 'user' ''',
            (stream_id,),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def interaction_density(self, stream_id: int, now: int | None = None) -> str:
        """根据最近三天消息数量生成自然语言互动密度描述。

        :param stream_id: 目标 stream ID。
        :param now: 可选当前 Unix 毫秒时间戳；省略时读取当前时钟。
        :return: 用于日程/提示词的中文密度描述。
        副作用：只读 messages 表。
        """
        now = now if now is not None else current_time()
        since = now - 3 * 24 * 60 * 60_000
        row = self._db.execute(
            'SELECT COUNT(*) FROM messages WHERE stream_id = ? AND created_at >= ?',
            (stream_id, since),
        ).fetchone()
        n = row[0] if row else 0
        if n >= 40:
            return '最近几天你们聊得很多，生活安排可以留出更多陪伴和放松的空白。'
        if n >= 10:
            return '最近几天你们偶尔聊聊，节奏自然而不拥挤。'
        return '最近几天互动很少，安排更偏向安静地做自己的事。'

    def pending_count(self, stream_id: int) -> int:
        """统计指定 stream 中尚未归档的消息数。

        :param stream_id: 目标 stream ID。
        :return: `episode_id IS NULL` 的消息数量。
        副作用：只读 messages 表。
        """
        row = self._db.execute(
            'SELECT COUNT(*) FROM messages WHERE stream_id = ? AND episode_id IS NULL',
            (stream_id,),
        ).fetchone()
        return row[0] if row else 0

    def assistant_reply_count_since(self, stream_id: int, since: int) -> int:
        """统计指定时间窗口内已落库的助手回复数量。

        :param stream_id: 目标 stream ID。
        :param since: 统计起点的 Unix 毫秒时间戳，包含该时刻。
        :return: 满足 stream、角色和时间条件的助手消息数量。
        :raises sqlite3.Error: 查询消息表失败时抛出。
        副作用：只读 messages 表。
        """
        row = self._db.execute(
            '''SELECT COUNT(*) FROM messages
               WHERE stream_id = ? AND role = 'assistant' AND created_at >= ?''',
            (stream_id, since),
        ).fetchone()
        return row[0] if row else 0

    def emoji_reply_count_since(self, stream_id: int, since: int) -> int:
        """统计窗口内实际选中表情包的助手消息数量。

        只有服务层确认命中可发送引用后，才会把 ``<emoji>`` 写入助手历史，
        因此该查询可直接复用现有消息窗口而不建立另一套频率状态。
        """

        row = self._db.execute(
            '''SELECT COUNT(*) FROM messages
               WHERE stream_id = ? AND role = 'assistant' AND created_at >= ?
                 AND instr(content, '<emoji ') > 0''',
            (stream_id, since),
        ).fetchone()
        return row[0] if row else 0

    def message_count_since(self, stream_id: int, since: int) -> int:
        """统计指定时间窗口内已落库的全部消息数量。

        :param stream_id: 目标 stream ID。
        :param since: 统计起点的 Unix 毫秒时间戳，包含该时刻。
        :return: 满足 stream 和时间条件的用户与助手消息总数。
        :raises sqlite3.Error: 查询消息表失败时抛出。
        副作用：只读 messages 表。
        """
        row = self._db.execute(
            '''SELECT COUNT(*) FROM messages
               WHERE stream_id = ? AND created_at >= ?''',
            (stream_id, since),
        ).fetchone()
        return row[0] if row else 0

    def oldest_pending(self, stream_id: int, n: int) -> list[dict[str, Any]]:
        """按消息 ID 正序读取指定数量的待归档消息。

        :param stream_id: 目标 stream ID。
        :param n: 最多返回的消息数量。
        :return: 包含 id、role、content、created_at 和 sender_person_id 的字典列表。
        副作用：只读 messages 表。
        """
        rows = self._db.execute(
            '''SELECT id, role, content, created_at, sender_person_id FROM messages
               WHERE stream_id = ? AND episode_id IS NULL ORDER BY id ASC LIMIT ?''',
            (stream_id, n)
        ).fetchall()
        return [{'id': r[0], 'role': r[1], 'content': r[2], 'created_at': r[3],
                 'sender_person_id': r[4]}
                for r in rows]

    # ------------------------------------------------------------------ L2 情节记忆
    def add_episode(self, stream_id: int, input: EpisodeInput, now: int | None = None) -> int:
        """写入一条情节摘要、召回线索并归档关联消息。

        :param stream_id: 目标 stream ID。
        :param input: 情节摘要、线索和待归档消息 ID。
        :param now: 可选创建时间戳；省略时读取当前毫秒时钟。
        :return: 新情节的数据库 ID。
        :raises sqlite3.Error: 情节、线索、FTS 或消息更新失败。
        副作用：写入 episodes、episode_cues、cues_fts，更新 messages.episode_id，
            并提交事务。
        """
        # 先写主记录取得 episode_id，后续线索和消息归档都依赖该外键。
        now = now if now is not None else current_time()
        cur = self._db.execute(
            '''INSERT INTO episodes (stream_id, kind, summary, started_at, ended_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (stream_id, input.kind, input.summary, input.started_at, input.ended_at, now)
        )
        episode_id = cur.lastrowid or 0

        # 空线索不进入 FTS，避免无意义的检索词占用索引行。
        for cue in input.cues:
            c = cue.strip()
            if not c:
                continue
            cue_cur = self._db.execute(
                'INSERT INTO episode_cues (episode_id, cue) VALUES (?, ?)', (episode_id, c)
            )
            cue_id = cue_cur.lastrowid or 0
            self._db.execute(
                'INSERT INTO cues_fts (rowid, tokens) VALUES (?, ?)', (cue_id, index_tokens(c))
            )

        # 关联消息在同一事务内归档，防止出现摘要已写入但原消息仍待处理的中间状态。
        if input.message_ids:
            placeholders = ','.join('?' * len(input.message_ids))
            self._db.execute(
                f'''UPDATE messages SET episode_id = ?
                    WHERE stream_id = ? AND id IN ({placeholders})''',
                (episode_id, stream_id, *input.message_ids)
            )
        self._db.commit()
        return episode_id

    def recent_episodes(self, stream_id: int, limit: int = 4) -> list[RecalledEpisode]:
        """读取指定 stream 最近结束的情节摘要。

        摘要失败留下的 ``UNSUMMARIZED_KIND`` 占位情节不参与召回：它只用于推进
        归档队列，正文是诊断信息而非对话内容，进入上下文只会污染工作记忆。

        :param stream_id: 目标 stream ID。
        :param limit: 最多返回的情节数，默认值为 4。
        :return: 按结束时间倒序排列的情节列表，分数固定为 1.0；不含占位情节。
        副作用：只读 episodes 表。
        """
        rows = self._db.execute(
            '''SELECT id, summary, kind, ended_at FROM episodes
               WHERE stream_id = ? AND kind != ? ORDER BY ended_at DESC LIMIT ?''',
            (stream_id, UNSUMMARIZED_KIND, limit)
        ).fetchall()
        return [RecalledEpisode(id=r[0], summary=r[1], kind=r[2], ended_at=r[3], score=1.0)
                for r in rows]

    def all_episodes(self, limit: int = 200) -> list[dict[str, Any]]:
        """读取所有 stream 最近的情节及其线索。

        :param limit: 最多读取的情节数量，默认值为 200。
        :return: 含 `id`、`kind`、`summary`、`ended_at`、`streamId` 和 `cues` 的字典列表。
        副作用：只读 episodes 和 episode_cues 表。
        """
        rows = self._db.execute(
            '''SELECT id, kind, summary, ended_at, stream_id FROM episodes
               ORDER BY ended_at DESC LIMIT ?''',
            (limit,)
        ).fetchall()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        placeholders = ','.join('?' * len(ids))
        cue_rows = self._db.execute(
            f'SELECT episode_id, cue FROM episode_cues WHERE episode_id IN ({placeholders})',
            ids
        ).fetchall()
        by_id: dict[int, list[str]] = {}
        for c in cue_rows:
            by_id.setdefault(c[0], []).append(c[1])
        return [{'id': r[0], 'kind': r[1], 'summary': r[2], 'ended_at': r[3],
                 'streamId': r[4],
                 'cues': by_id.get(r[0], [])} for r in rows]

    def recall_episodes(self, stream_id: int, query: str, limit: int = 3) -> list[RecalledEpisode]:
        """使用 cues FTS5 召回指定 stream 的相关情节。

        :param stream_id: 目标 stream ID。
        :param query: 待匹配的自然语言查询。
        :param limit: 最多返回的情节数，默认值为 3。
        :return: 按 BM25 归一化分数降序截取的情节列表；查询无有效词时返回空列表。
        :raises sqlite3.Error: FTS 查询失败。
        副作用：只读 FTS 和情节表。
        """
        match = match_query(query)
        if not match:
            return []
        rows = self._db.execute(
            '''SELECT e.id, e.summary, e.kind, e.ended_at, bm25(cues_fts) AS bm
               FROM cues_fts
               JOIN episode_cues c ON c.id = cues_fts.rowid
               JOIN episodes e     ON e.id = c.episode_id
               WHERE cues_fts MATCH ? AND e.stream_id = ?
               ORDER BY bm ASC LIMIT ?''',
            (match, stream_id, limit * 4)
        ).fetchall()
        best: dict[int, RecalledEpisode] = {}
        for r in rows:
            if r[0] not in best:
                best[r[0]] = RecalledEpisode(
                    id=r[0], summary=r[1], kind=r[2], ended_at=r[3], score=score(r[4], 1.0)
                )
        return list(best.values())[:limit]

    # ------------------------------------------------------------------ L3 语义记忆
    def add_fact(self, person_id: int, input: FactInput, now: int | None = None) -> FactWrite:
        """新增、强化或取代人物的一条语义事实。

        三条规则：

        1. 完全重复只强化留存度，不新增重复正文，保持同一人物的事实唯一性；
        2. ``input.supersedes`` 显式声明取代时，写新行并在同一事务里回填旧行的
           ``superseded_by``——取代是唯一让事实失效的入口；
        3. ``slot`` 非空且同一 ``(person_id, slot)`` 已有其他活跃事实时，两条都保留，
           返回值里的 ``conflict_with`` 让调用方知道发生了冲突。冲突只被看见，
           不按时间取新、不按分数取高、不在写入侧二选一。

        :param person_id: 事实所属人物 ID。
        :param input: 事实正文、类型与账本字段。
        :param now: 可选更新时间戳；省略时读取当前毫秒时钟。
        :return: 本次写入结果；正文归一化后为空时 ``fact_id`` 为 ``0``。
        :raises sqlite3.Error: 查询、插入、更新、FTS 写入或提交失败。
        副作用：可能更新已有事实强度，或写入 facts 与 facts_fts 并提交事务。
        """
        # 先规范化正文和去重键，空内容不创建事实记录。
        now = now if now is not None else current_time()
        content = input.content.strip()
        if not content:
            return FactWrite(fact_id=0)
        key = exact_key(content)
        if not key:
            return FactWrite(fact_id=0)
        slot = input.slot.strip()
        supersedes = input.supersedes if input.supersedes > 0 else 0
        half_life = half_life_for(input.kind)

        existing = self._find_similar(person_id, content, key)
        if existing:
            # 相似事实只强化留存度，不新增重复正文，保持同一人物的事实唯一性。
            cur_ret = retention(existing['strength'], existing['updated_at'], existing['half_life_hours'], now)
            next_strength = reinforce(cur_ret)
            self._db.execute(
                '''UPDATE facts SET strength = ?, updated_at = ?, due_at = ?, active = 1,
                                     hit_count = hit_count + 1 WHERE id = ? AND person_id = ?''',
                (
                    next_strength,
                    now,
                    freeze_due_at(next_strength, now, existing['half_life_hours']),
                    existing['id'],
                    person_id,
                )
            )
            # 强化命中不取消显式取代的声明：新正文恰好与既有事实同义时，
            # 被取代行改由这条既有事实接续。
            superseded = (
                self._apply_supersede(person_id, supersedes, replaced_by=existing['id'])
                if supersedes else 0
            )
            self._db.commit()
            return FactWrite(fact_id=existing['id'], created=False, superseded=superseded)

        # 未命中相似事实时创建新记录；新行、取代回填与 FTS 索引在同一事务里提交。
        due = freeze_due_at(1.0, now, half_life)
        cur = self._db.execute(
            '''INSERT INTO facts (person_id, kind, content, content_key, strength, half_life_hours,
                                  updated_at, created_at, due_at, active, slot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)''',
            (person_id, input.kind, content, key, 1.0, half_life, now, now, due, slot)
        )
        fid = cur.lastrowid or 0
        self._db.execute(
            'INSERT INTO facts_fts (rowid, tokens) VALUES (?, ?)', (fid, index_tokens(content))
        )
        superseded = (
            self._apply_supersede(person_id, supersedes, replaced_by=fid)
            if supersedes else 0
        )
        conflict_with = (
            self._slot_conflicts_with(person_id, slot, exclude_id=fid)
            if slot else []
        )
        self._db.commit()
        return FactWrite(
            fact_id=fid,
            created=True,
            conflict_with=conflict_with,
            superseded=superseded,
        )

    def _apply_supersede(self, person_id: int, target_id: int, *, replaced_by: int) -> int:
        """把同属一人的旧行回填为由新行取代；目标不可写时不改任何行并返回 ``0``。

        守卫条件缺一不可：同一人物、尚未被取代、不是取代者自己——
        模型可能编造或错指 ID，越界的取代绝不能落到无关的行上。
        """

        if target_id == replaced_by:
            return 0
        cur = self._db.execute(
            '''UPDATE facts SET superseded_by = ?
               WHERE id = ? AND person_id = ? AND superseded_by IS NULL''',
            (replaced_by, target_id, person_id),
        )
        return target_id if cur.rowcount else 0

    def _slot_conflicts_with(self, person_id: int, slot: str, *, exclude_id: int) -> list[int]:
        """读取同一 ``(person_id, slot)`` 下仍活跃的其他事实 ID，供冲突上报。"""

        rows = self._db.execute(
            '''SELECT id FROM facts
               WHERE person_id = ? AND slot = ? AND active = 1
                 AND superseded_by IS NULL AND id <> ?
               ORDER BY id''',
            (person_id, slot, exclude_id),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def slot_conflicts(
        self,
        person_id: int,
        fact_ids: Iterable[int],
    ) -> dict[int, tuple[str, list[tuple[int, str]]]]:
        """读取给定事实所属的槽位冲突组，供提示词把对不上的几条并排渲染。

        冲突组定义为同一 ``(person_id, slot)`` 下至少两条活跃且未被取代的事实；
        ``slot`` 为空的多值事实永不入组。

        :param person_id: 事实所属人物 ID。
        :param fact_ids: 待检查的事实 ID 集合，通常为即将注入提示词的那批。
        :return: ``fact_id -> (slot, [(成员 ID, 成员正文), ...])``；成员列表含该
            槽位下全部活跃事实（包括传入者自身），按 ID 升序。不在映射里的
            事实没有冲突。
        :raises sqlite3.Error: 查询失败。
        副作用：只读 facts 表。
        """

        wanted = {int(fact_id) for fact_id in fact_ids}
        if not wanted:
            return {}
        rows = self._db.execute(
            '''SELECT id, slot, content FROM facts
               WHERE person_id = ? AND active = 1 AND superseded_by IS NULL AND slot <> ''
               ORDER BY id''',
            (person_id,),
        ).fetchall()
        by_slot: dict[str, list[tuple[int, str]]] = {}
        for row_id, slot, content in rows:
            by_slot.setdefault(str(slot), []).append((int(row_id), str(content)))
        result: dict[int, tuple[str, list[tuple[int, str]]]] = {}
        for slot, members in by_slot.items():
            if len(members) < 2:
                continue
            member_ids = {member_id for member_id, _ in members}
            for fact_id in wanted & member_ids:
                result[fact_id] = (slot, members)
        return result

    def _find_similar(self, person_id: int, content: str, key: str) -> dict[str, Any] | None:
        """按精确键和 FTS 候选查找同一人物的相似事实。

        :param person_id: 事实所属人物 ID。
        :param content: 已去空白的待比较正文。
        :param key: `exact_key(content)` 生成的严格去重键。
        :return: 含事实 ID、正文、强度和衰减参数的字典；没有相似事实时返回 `None`。
        :raises sqlite3.Error: 查询失败。
        副作用：只读 facts 和 facts_fts 表。
        :performance: 精确键优先；未命中时最多检查 8 个 FTS 候选。
        """
        # 精确键必须不带失效过滤：``UNIQUE(person_id, content_key)`` 约束要求
        # 同键必命中既有行，否则同文重提会在插入时撞上唯一约束。
        row = self._db.execute(
            '''SELECT id, content, strength, updated_at, half_life_hours
               FROM facts WHERE person_id = ? AND content_key = ?''', (person_id, key)
        ).fetchone()
        if row:
            return {'id': row[0], 'content': row[1], 'strength': row[2],
                    'updated_at': row[3], 'half_life_hours': row[4]}
        match = match_query(content)
        if not match:
            return None
        # FTS 候选排除已被取代的行：与失效事实措辞相近的新表述应当另起一行
        # （同槽时形成可见冲突），而不是把留存度回补到一条不再召回的死行上。
        candidates = self._db.execute(
            '''SELECT f.id, f.content, f.strength, f.updated_at, f.half_life_hours
               FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
               WHERE facts_fts MATCH ? AND f.person_id = ? AND f.superseded_by IS NULL
               ORDER BY bm25(facts_fts) ASC LIMIT 8''',
            (match, person_id)
        ).fetchall()
        for c in candidates:
            if is_same_fact(content, c[1]):
                return {'id': c[0], 'content': c[1], 'strength': c[2],
                        'updated_at': c[3], 'half_life_hours': c[4]}
        return None

    def _emit_scope_blocked(self, stream_kind: str, blocked: int) -> None:
        """登记一次可见性拦截，供「她怎么突然不记得了」与「本来就没有」区分。

        :param stream_kind: 本次读取发生的会话类型。
        :param blocked: 被挡下的事实条数，恒大于零。
        副作用：发出一条 ``memory_fact_scope_blocked`` 观测事件，不写库。
        """

        trace.emit('memory_fact_scope_blocked', streamKind=stream_kind, blocked=blocked)

    def recall_facts(
        self,
        person_id: int,
        query: str,
        limit: int = 6,
        now: int | None = None,
        query_embedding: bytes | None = None,
        reinforce_matches: bool = True,
        return_candidates: bool = False,
        *,
        stream_kind: str,
        private_in_group: bool = False,
    ) -> list[RecalledFact]:
        """
        按 BM25 召回人物事实，并在向量齐全时执行混合相关度排序。

        已被取代（``superseded_by`` 非空）的事实不进入候选：失效行只留在库里
        构成取代链，不再被任何召回入口返回。

        :param person_id: 目标人物 ID。
        :param query: 待检索的自然语言文本。
        :param limit: 最多返回的事实数量，默认 ``6``。
        :param now: 可选当前 Unix 毫秒时间戳；省略时读取当前时钟。
        :param query_embedding: 查询文本的小端 float32 packed 向量；为 ``None`` 时仅使用 BM25。
        :param reinforce_matches: 是否回补命中事实；动作决策预览应传 ``False``。
        :param return_candidates: 是否返回截取前的候选池，供同一轮后续向量重排。
        :param stream_kind: 当前读取发生的会话类型；必填，漏传会让
            ``direct`` 事实无声地出现在群聊提示词里。
        :param private_in_group: ``conversation.private_facts_in_group`` 的当前值；
            默认关闭，即群聊里挡下私聊来源的事实。

        :return: 按混合相关度降序排列的事实列表；无有效查询词时返回空列表。

        :raises sqlite3.Error: FTS 查询、事实更新或事务提交失败。
        :raises struct.error: 查询向量与事实向量维度不匹配时，向量分支捕获该错误并回退 BM25。

        副作用：
            读取 FTS 和 facts 表；对最终命中的事实回补强度、更新时间、命中次数并提交事务；
            有事实被可见性规则挡下时发出一条 ``memory_fact_scope_blocked`` 事件。

        性能：
            最多读取 ``limit * 3`` 个 FTS 候选，可见性过滤发生在截断之前；
            向量融合仅在查询向量和事实向量同时存在时执行。
        """
        now = now if now is not None else current_time()
        match = match_query(query)
        if not match:
            return []
        rows = self._db.execute(
            '''SELECT f.id, f.kind, f.content, f.strength, f.updated_at,
                      f.half_life_hours, f.active, bm25(facts_fts) AS bm,
                      f.embedding, f.origin_kind
               FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
               WHERE facts_fts MATCH ? AND f.person_id = ? AND f.superseded_by IS NULL
               ORDER BY bm ASC LIMIT ?''',
            (match, person_id, limit * 3)
        ).fetchall()
        blocked = 0
        visible_rows = []
        for r in rows:
            if fact_visible_in_stream(
                r[9], stream_kind, private_in_group=private_in_group,
            ):
                visible_rows.append(r)
            else:
                blocked += 1
        if blocked:
            self._emit_scope_blocked(stream_kind, blocked)
        scored = []
        for r in visible_rows:
            ret = retention(r[3], r[4], r[5], now)
            relevance = relevance_from_bm25(r[7])
            # 只有两侧向量都存在时才融合相关度，缺失任一向量则保留 BM25 结果。
            #
            # 原因：
            # 1. BM25 对专名、数字和代码关键字的精确匹配不可由语义相似度完全替代。
            # 2. 留存度必须在 BM25/向量融合后统一施加，否则向量分支会绕过事实遗忘曲线。
            # 当前处理：以 0.4/0.6 融合词面和语义相关度，再乘以留存度权重。
            fact_embedding = r[8]
            if query_embedding is not None and fact_embedding is not None:
                try:
                    from .embed import cosine
                    # 查询向量按 float32 打包，每个分量占 4 字节；维度必须与解包结果一致。
                    dim = len(query_embedding) // 4
                    cos = cosine(query_embedding, fact_embedding, dim)
                    # 余弦相似度范围为 [-1, 1]，映射到 [0, 1] 后与 BM25 使用同一分数域。
                    vec_score = (cos + 1) / 2
                    relevance = 0.4 * relevance + 0.6 * vec_score
                except Exception:
                    # 向量计算失败时保留 BM25 相关度，确保单条坏向量不阻断整批召回。
                    pass
            final_score = relevance * retention_weight(ret)
            scored.append(RecalledFact(
                id=r[0],
                kind=r[1],
                content=r[2],
                retention=ret,
                score=final_score,
                lexical_relevance=relevance_from_bm25(r[7]),
                embedding=r[8],
                half_life_hours=r[5],
            ))
        scored.sort(key=lambda x: x.score, reverse=True)
        selected = scored[:limit]
        result = scored if return_candidates else selected
        # 命中后回补事实强度，使重复访问逐步提高留存度。
        if reinforce_matches:
            for h in selected:
                row = next((r for r in rows if r[0] == h.id), None)
                if row:
                    nxt = reinforce(h.retention)
                    self._db.execute(
                        '''UPDATE facts SET strength = ?, updated_at = ?, due_at = ?, active = 1,
                                             hit_count = hit_count + 1, last_hit_at = ?
                           WHERE id = ? AND person_id = ?''',
                        (nxt, now, freeze_due_at(nxt, now, row[5]), now, h.id, person_id)
                    )
        if selected and reinforce_matches:
            self._db.commit()
        return result

    def recall_facts_in_scope(
        self,
        person_ids: Sequence[int],
        query: str,
        limit: int = 6,
        now: int | None = None,
        *,
        stream_kind: str,
        private_in_group: bool = False,
        return_candidates: bool = False,
    ) -> list[ScopedFact]:
        """在若干人物范围内一次性召回事实，供认知动作按会话在场者检索。

        与 :meth:`recall_facts` 的两点差别都是有意的：

        1. 一次查询覆盖多人。群聊里在场者可能有十几个，逐人调用会把一次
           检索放大成十几次 FTS 查询。
        2. 不回补强度。决策期的主动检索若参与遗忘曲线，检索动作本身会改写
           记忆权重，同一条事实被反复 recall 后不再衰减。
           写回只应发生在真实使用（回复里确实用上了）时，不在检索时。
        3. 已被取代（``superseded_by`` 非空）的事实不进入候选。

        :param person_ids: 检索范围内的人物 ID 序列；为空时直接返回空列表。
        :param query: 待检索的自然语言文本。
        :param limit: 最多返回的事实数量，默认 ``6``。
        :param now: 可选当前 Unix 毫秒时间戳；省略时读取当前时钟。
        :param stream_kind: 当前读取发生的会话类型；必填，漏传会让
            ``direct`` 事实无声地出现在群聊提示词里。
        :param private_in_group: ``conversation.private_facts_in_group`` 的当前值。
        :param return_candidates: 是否返回截取前的候选池，供同一轮的向量重排
            与多检索词并集合并；候选随 ``embedding`` 一并带回。

        :return: 按留存度加权相关度降序排列的事实列表；无有效查询词时为空列表。

        :raises sqlite3.Error: FTS 查询失败。

        副作用：只读 facts_fts 与 facts 表，不写任何列、不提交事务；
            有事实被可见性规则挡下时发出一条 ``memory_fact_scope_blocked`` 事件。

        性能：单次 FTS 查询，最多读取 ``limit * 3`` 个候选。
        """
        scope = tuple(dict.fromkeys(person_ids))
        if not scope:
            return []
        match = match_query(query)
        if not match:
            return []
        now = now if now is not None else current_time()
        placeholders = ','.join('?' for _ in scope)
        rows = self._db.execute(
            f'''SELECT f.id, f.kind, f.content, f.strength, f.updated_at,
                       f.half_life_hours, f.person_id, bm25(facts_fts) AS bm,
                       f.origin_kind, f.embedding
                FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
                WHERE facts_fts MATCH ? AND f.person_id IN ({placeholders})
                  AND f.superseded_by IS NULL
                ORDER BY bm ASC LIMIT ?''',
            (match, *scope, limit * 3),
        ).fetchall()
        blocked = 0
        visible_rows = []
        for r in rows:
            if fact_visible_in_stream(
                r[8], stream_kind, private_in_group=private_in_group,
            ):
                visible_rows.append(r)
            else:
                blocked += 1
        if blocked:
            self._emit_scope_blocked(stream_kind, blocked)
        scored: list[ScopedFact] = []
        for r in visible_rows:
            ret = retention(r[3], r[4], r[5], now)
            relevance = relevance_from_bm25(r[7])
            scored.append(ScopedFact(
                id=r[0],
                kind=r[1],
                content=r[2],
                retention=ret,
                score=relevance * retention_weight(ret),
                lexical_relevance=relevance,
                embedding=r[9],
                half_life_hours=r[5],
                person_id=r[6],
            ))
        scored.sort(key=lambda fact: fact.score, reverse=True)
        return scored if return_candidates else scored[:limit]

    def message_count_after(self, stream_id: int, since_id: int) -> int:
        """统计某条消息之后该 stream 又落库了多少条消息。

        场景观察据此节流：条数本身就是节流器，消息来得慢自然算得少。
        计数有意包含助手动作伪消息：它们虽然不进入表达学习与事实抽取的模型正文，
        仍是已经发生的历史事件。为这一小量偏差再维护按内容过滤的第二套游标口径，
        会让摘要、观察与学习阈值互相牵制，因此统一按 messages 行数推进。

        :param stream_id: 目标 stream ID。
        :param since_id: 起点消息 ID（不含）；``0`` 表示统计全部。
        :return: 该消息之后的消息条数。
        :raises sqlite3.Error: 查询失败。
        副作用：只读 messages 表。
        """
        row = self._db.execute(
            'SELECT COUNT(*) FROM messages WHERE stream_id = ? AND id > ?',
            (stream_id, since_id),
        ).fetchone()
        return int(row[0])

    def messages_after(self, stream_id: int, since_id: int, limit: int) -> list[StoredMessage]:
        """按 ID 顺序取某条消息之后的一批消息，不受摘要归档状态影响。

        与 :meth:`working_memory` / :meth:`oldest_pending` 的差别是有意的：那两个都按
        ``episode_id IS NULL`` 判定「待处理」，消息一旦被摘要归档就此不可见。事实抽取是
        第二个独立的消费者，若共用同一判据，两者会相互消费对方的输入且不报错——因此它按自己的
        游标取消息，与摘要队列完全解耦。

        :param stream_id: 目标 stream ID。
        :param since_id: 起点消息 ID（不含）；``0`` 表示从头开始。
        :param limit: 最多返回的消息条数。
        :return: 按 ID 正序排列的 `StoredMessage` 列表。
        :raises sqlite3.Error: 查询失败。
        副作用：只读 messages 表，不写任何列。
        """
        rows = self._db.execute(
            '''SELECT id, role, content, created_at, sender_person_id FROM messages
               WHERE stream_id = ? AND id > ? ORDER BY id ASC LIMIT ?''',
            (stream_id, since_id, limit),
        ).fetchall()
        return [
            StoredMessage(
                message_id=r[0],
                role=r[1],
                content=r[2],
                created_at=r[3],
                sender_person_id=r[4],
            )
            for r in rows
        ]

    def recent_speakers(
        self,
        stream_id: int,
        before_id: int,
        scan_limit: int = 200,
    ) -> list[int]:
        """列出该 stream 最近开口过的人物 ID，供认知检索确定「在场者」范围。

        群聊里 Bot 可能被问到第三个人的事，把事实检索范围收窄成「本批发言者」会让
        recall 在最需要的场景下召回为空；反过来放开到全库又跨越了会话隐私边界。
        取「本 stream 最近若干条消息的发言者」是两者之间唯一有事实依据的口径。

        :param stream_id: 目标 stream ID。
        :param before_id: 只统计该消息 ID 及之前的消息，与回合水位对齐。
        :param scan_limit: 回溯的消息条数上界，默认 ``200``。
        :return: 按最近发言优先排列、去重后的人物 ID 列表。
        :raises sqlite3.Error: 查询失败。
        副作用：只读 messages 表。
        """
        rows = self._db.execute(
            '''SELECT sender_person_id FROM (
                   SELECT sender_person_id, id FROM messages
                   WHERE stream_id = ? AND id <= ? AND sender_person_id IS NOT NULL
                   ORDER BY id DESC LIMIT ?
               )''',
            (stream_id, before_id, scan_limit),
        ).fetchall()
        return list(dict.fromkeys(int(row[0]) for row in rows))

    def latest_message_id(self, stream_id: int) -> int:
        """返回该 stream 已落库的最大消息 ID。

        供需要「不设上界」的 :meth:`recent_speakers` 调用方使用——它的 ``before_id``
        是闭区间上界，用一个魔数当无穷大会让调用点读不出意图。

        :param stream_id: 目标 stream ID。
        :return: 最大消息 ID；该 stream 还没有消息时返回 ``0``。
        :raises sqlite3.Error: 查询失败。
        副作用：只读 messages 表。
        """
        row = self._db.execute(
            'SELECT MAX(id) FROM messages WHERE stream_id = ?', (stream_id,)
        ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    def search_messages(
        self,
        stream_id: int,
        query: str,
        before_id: int,
        limit: int = 6,
    ) -> list[StoredMessage]:
        """在指定 stream 的历史消息里按词命中检索，供认知动作检索更早的聊天。

        不建 FTS 索引是有意的：messages 没有 FTS 表，新建一张要连带触发器与
        迁移，而 facts_fts 的 rowid 对齐问题在迁移中已出现过（迁移必须显式搬 id）。
        这里改用「分词 → LIKE 预筛 → Python 侧按命中词数打分」，预筛有硬上界
        ``_MESSAGE_SCAN_LIMIT``，代价可控且行为可单测。命中词数相同时按时间靠后优先，
        最近提及的那次几乎总是检索目标。

        :param stream_id: 目标 stream ID。
        :param query: 待检索的自然语言文本。
        :param before_id: 只检索该消息 ID 之前（不含）的历史；调用方传回合水位，
            使检索结果不会包含本批尚未处理的消息。
        :param limit: 最多返回的消息数，默认 ``6``。

        :return: 按时间正序排列的消息列表；无有效查询词或无命中时为空列表。

        :raises sqlite3.Error: 查询失败。

        副作用：只读 messages 表。

        性能：一次 LIKE 预筛最多返回 ``_MESSAGE_SCAN_LIMIT`` 行，打分在内存完成。
        """
        # 只取前若干个查询词：LIKE 条件是 OR 拼接，词越多预筛越接近全表扫描，
        # 而超出部分对命中排序的贡献迅速趋近于零。
        terms = list(dict.fromkeys(words(query)))[:_MESSAGE_QUERY_TERMS]
        if not terms:
            return []
        conditions = ' OR '.join('content LIKE ?' for _ in terms)
        patterns = [f'%{term}%' for term in terms]
        rows = self._db.execute(
            f'''SELECT id, role, content, created_at, sender_person_id FROM messages
                WHERE stream_id = ? AND id < ? AND ({conditions})
                ORDER BY id DESC LIMIT ?''',
            (stream_id, before_id, *patterns, _MESSAGE_SCAN_LIMIT),
        ).fetchall()
        scored: list[tuple[int, int, StoredMessage]] = []
        for r in rows:
            lowered = r[2].lower()
            hits = sum(1 for term in terms if term in lowered)
            if hits == 0:
                continue
            scored.append((hits, r[0], StoredMessage(
                role=r[1],
                content=r[2],
                created_at=r[3],
                sender_person_id=r[4],
                message_id=r[0],
            )))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = [message for _, _, message in scored[:limit]]
        selected.sort(key=lambda message: message.message_id)
        return selected

    def rank_recalled_facts(
        self,
        candidates: list[RecalledFact],
        query_embedding: bytes | None,
        limit: int,
    ) -> list[RecalledFact]:
        """在已召回候选上附加向量相关度，不重新查询数据库。

        :param candidates: 同一轮决策前通过 :meth:`recall_facts` 取得的候选池。
        :param query_embedding: 查询文本的小端 float32 packed 向量；为 ``None`` 时保留词面排序。
        :param limit: 最多返回的事实数量。
        :return: 按增强后分数降序截取的原候选对象列表。
        :raises ValueError: ``limit`` 小于零。
        副作用：不读写数据库，不修改传入候选对象。
        """
        if limit < 0:
            raise ValueError('事实召回上限不能小于零')
        ranked: list[tuple[float, RecalledFact]] = []
        for fact in candidates:
            relevance = fact.lexical_relevance
            if query_embedding is not None and fact.embedding is not None:
                try:
                    from .embed import cosine
                    dim = len(query_embedding) // 4
                    cosine_score = cosine(query_embedding, fact.embedding, dim)
                    relevance = 0.4 * relevance + 0.6 * ((cosine_score + 1) / 2)
                except Exception:
                    # 与召回入口保持一致：单条坏向量只放弃该条语义融合。
                    pass
            ranked.append((relevance * retention_weight(fact.retention), fact))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [fact for _, fact in ranked[:limit]]

    def reinforce_recalled_facts(
        self,
        facts: Sequence[ScopedFact],
        now: int | None = None,
    ) -> None:
        """强化同一轮最终实际用于回复的事实，不再次执行召回。

        回补按每条事实自带的 ``person_id`` 落行：召回范围扩到在场多人后，
        继续按单一人物回补会把别人的事实强度全记到当前说话人头上。

        :param facts: 已确认用于回复的召回事实名单，携带各自归属。
        :param now: 可选当前 Unix 毫秒时间戳；省略时读取当前时钟。
        :raises sqlite3.Error: 更新或提交失败。
        副作用：按传入事实 ID 更新强度、命中次数和下次评估时间并提交。
        """
        if not facts:
            return
        now = now if now is not None else current_time()
        for fact in facts:
            next_strength = reinforce(fact.retention)
            self._db.execute(
                '''UPDATE facts SET strength = ?, updated_at = ?, due_at = ?, active = 1,
                                     hit_count = hit_count + 1, last_hit_at = ?
                   WHERE id = ? AND person_id = ?''',
                (
                    next_strength,
                    now,
                    freeze_due_at(next_strength, now, fact.half_life_hours),
                    now,
                    fact.id,
                    fact.person_id,
                ),
            )
        self._db.commit()

    def store_embedding(self, fact_id: int, embedding: bytes) -> None:
        """为指定事实写入已打包的向量数据。

        :param fact_id: ``facts`` 表中的事实 ID。
        :param embedding: 小端 float32 packed 向量字节串；维度由调用方保证与索引一致。

        :raises sqlite3.Error: 更新或提交失败。

        副作用：
            更新 ``facts.embedding`` 并提交事务；不存在的 fact ID 不会新增记录。
        """
        self._db.execute('UPDATE facts SET embedding = ? WHERE id = ?', (embedding, fact_id))
        self._db.commit()

    def facts_without_embedding(self, limit: int = 128) -> list[dict]:
        """读取尚未计算 embedding 的事实摘要，供后台批量补算。

        :param limit: 最多返回的事实数量，默认 ``128``；应为非负整数。

        :return: 包含 ``id`` 和 ``content`` 字段的事实字典列表。

        :raises sqlite3.Error: 查询失败。

        副作用：
            只读 ``facts`` 表，不修改记录或提交事务。
        """
        rows = self._db.execute(
            'SELECT id, content FROM facts WHERE embedding IS NULL LIMIT ?', (limit,)
        ).fetchall()
        return [{'id': r[0], 'content': r[1]} for r in rows]

    def sweep(self, now: int | None = None) -> int:
        """评估到期事实并冻结留存度低于阈值的记录。

        :param now: 可选当前 Unix 毫秒时间戳；省略时读取当前时钟。
        :return: 本次转为非活跃的事实数量。
        :raises sqlite3.Error: 查询、更新或提交失败。
        副作用：更新 facts.active 和 due_at，并提交事务。
        """
        now = now if now is not None else current_time()
        due = self._db.execute(
            'SELECT id, strength, updated_at, half_life_hours, active FROM facts WHERE active = 1 AND due_at <= ?',
            (now,)
        ).fetchall()
        if not due:
            return 0
        n = 0
        for f in due:
            ev = evaluate(DecayState(strength=f[1], updated_at=f[2], half_life_hours=f[3], active=bool(f[4])), now)
            if not ev.active:
                self._db.execute('UPDATE facts SET active = 0, due_at = ? WHERE id = ?', (ev.due_at, f[0]))
                n += 1
        self._db.commit()
        return n

    def top_facts(self, person_id: int, limit: int = 8,
                  now: int | None = None, *,
                  stream_kind: str,
                  private_in_group: bool = False) -> list[RecalledFact]:
        """按当前留存度返回人物的活跃事实。

        已被取代（``superseded_by`` 非空）的事实不返回：它只留在库里构成取代链。

        :param person_id: 目标人物 ID。
        :param limit: 最多返回的事实数，默认值为 8。
        :param now: 可选当前 Unix 毫秒时间戳；省略时读取当前时钟。
        :param stream_kind: 当前读取发生的会话类型；唯一例外是抽取去重清单
            传旁路值 ``'all'``——清单的目的是去重不是输出，必须看到全部事实。
        :param private_in_group: ``conversation.private_facts_in_group`` 的当前值。
        :return: 按留存度降序排列的事实列表。
        副作用：只读 facts 表，不执行命中回补；有事实被可见性规则挡下时
            发出一条 ``memory_fact_scope_blocked`` 事件。
        """
        now = now if now is not None else current_time()
        rows = self._db.execute(
            '''SELECT id, kind, content, strength, updated_at, half_life_hours,
                      origin_kind
               FROM facts
               WHERE person_id = ? AND active = 1 AND superseded_by IS NULL''',
            (person_id,)
        ).fetchall()
        blocked = 0
        visible_rows = []
        for r in rows:
            if stream_kind == SCOPE_ALL or fact_visible_in_stream(
                r[6], stream_kind, private_in_group=private_in_group,
            ):
                visible_rows.append(r)
            else:
                blocked += 1
        if blocked:
            self._emit_scope_blocked(stream_kind, blocked)
        result = []
        for r in visible_rows:
            ret = retention(r[3], r[4], r[5], now)
            result.append(RecalledFact(id=r[0], kind=r[1], content=r[2], retention=ret, score=ret))
        result.sort(key=lambda x: x.score, reverse=True)
        return result[:limit]

    def all_facts(self, person_id: int, now: int | None = None) -> list[StoredFact]:
        """读取人物的全部事实并计算当前留存度和冻结状态。

        :param person_id: 目标人物 ID。
        :param now: 可选当前 Unix 毫秒时间戳；省略时读取当前时钟。
        :return: 按留存度降序排列的完整事实列表。
        副作用：只读 facts 表，不改变 active 字段。
        """
        now = now if now is not None else current_time()
        rows = self._db.execute(
            '''SELECT id, kind, content, strength, updated_at, half_life_hours, due_at, active
               FROM facts WHERE person_id = ?''',
            (person_id,)
        ).fetchall()
        result = []
        for r in rows:
            ret = retention(r[3], r[4], r[5], now)
            result.append(StoredFact(
                id=r[0], kind=r[1], content=r[2], retention=ret, score=ret,
                due_at=r[6], frozen=(r[7] == 0 or ret <= FREEZE)
            ))
        result.sort(key=lambda x: x.retention, reverse=True)
        return result

    def fact_count(self, person_id: int) -> dict[str, int]:
        """统计人物事实总数和当前活跃数。

        :param person_id: 目标人物 ID。
        :return: 含 `total` 和 `active` 两个整数键的字典。
        副作用：只读 facts 表。
        """
        row = self._db.execute(
            'SELECT COUNT(*), SUM(active) FROM facts WHERE person_id = ?', (person_id,)
        ).fetchone()
        return {'total': row[0] or 0, 'active': row[1] or 0}

    # ------------------------------------------------------------------ 待说的话
    def queue_utterance(self, source: str, text: str, deliver_after: int,
                        expires_at: int, emotion: str | None = None,
                        now: int | None = None) -> int:
        """向待说话队列表写入一条带投放窗口的文本。

        :param source: 产生该文本的来源标识。
        :param text: 待投放正文；首尾空白会移除，规范化后为空时不写入。
        :param deliver_after: 最早允许投放的 Unix 毫秒时间戳。
        :param expires_at: 投放截止 Unix 毫秒时间戳。
        :param emotion: 可选情绪标识。
        :param now: 可选创建时间戳；省略时读取当前毫秒时钟。

        :return: 新建待说话记录的 ID；文本为空时返回 ``0``。

        :raises sqlite3.Error: 插入或提交失败。

        副作用：
            写入 ``pending_utterances`` 并提交事务。
        """
        now = now if now is not None else current_time()
        text = text.strip()
        if not text:
            return 0
        cur = self._db.execute(
            '''INSERT INTO pending_utterances (source, emotion, text, deliver_after, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (source, emotion, text, deliver_after, expires_at, now)
        )
        self._db.commit()
        return cur.lastrowid or 0

    def due_utterances(self, now: int | None = None, limit: int = 4) -> list[dict[str, Any]]:
        """读取当前已到投放时间且尚未过期的待说话记录。

        :param now: 可选当前 Unix 毫秒时间戳；省略时读取当前时钟。
        :param limit: 最多返回的记录数，默认 ``4``。

        :return: 按记录 ID 升序排列、包含 ``id``、``source``、``emotion`` 和 ``text`` 字段的列表。

        :raises sqlite3.Error: 查询失败。

        副作用：
            只读 ``pending_utterances`` 表，不标记记录已投放。
        """
        now = now if now is not None else current_time()
        rows = self._db.execute(
            '''SELECT id, source, emotion, text FROM pending_utterances
               WHERE delivered_at IS NULL AND deliver_after <= ? AND expires_at > ?
               ORDER BY id ASC LIMIT ?''',
            (now, now, limit)
        ).fetchall()
        return [{'id': r[0], 'source': r[1], 'emotion': r[2], 'text': r[3]} for r in rows]

    def mark_delivered(self, ids: list[int], now: int | None = None) -> None:
        """将指定待说话记录标记为已投放。

        :param ids: 待更新的记录 ID 列表；空列表不执行数据库操作。
        :param now: 可选投放时间戳；省略时读取当前毫秒时钟。

        :raises sqlite3.Error: 更新或提交失败。

        副作用：
            更新匹配记录的 ``delivered_at`` 并提交事务；不存在的 ID 被忽略。
        """
        if not ids:
            return
        now = now if now is not None else current_time()
        placeholders = ','.join('?' * len(ids))
        self._db.execute(
            f'UPDATE pending_utterances SET delivered_at = ? WHERE id IN ({placeholders})',
            (now, *ids)
        )
        self._db.commit()

    def has_queued_since(self, source: str, since: int) -> bool:
        """判断指定来源在给定时间之后是否创建过待说话记录。

        :param source: 待匹配的来源标识。
        :param since: 起始 Unix 毫秒时间戳，包含该时刻。

        :return: 存在满足条件的记录时返回 ``True``，否则返回 ``False``。

        :raises sqlite3.Error: 查询失败。
        """
        row = self._db.execute(
            'SELECT 1 FROM pending_utterances WHERE source = ? AND created_at >= ? LIMIT 1',
            (source, since)
        ).fetchone()
        return row is not None

    def pending_utterance_count(self) -> int:
        """统计尚未标记为已投放的待说话记录数量。

        :return: ``delivered_at IS NULL`` 的待说话记录数量。

        :raises sqlite3.Error: 查询失败。
        """
        row = self._db.execute(
            'SELECT COUNT(*) FROM pending_utterances WHERE delivered_at IS NULL'
        ).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------ meta JSON 键值
    def load_pending_promises(self) -> list[dict[str, Any]]:
        """读取持久化的跨重启约定列表。

        :return: 仅包含字典项的约定列表；meta 值缺失、解析失败或顶层不是列表时返回空列表。

        :raises sqlite3.Error: 读取 meta 表失败。
        """
        raw = self.read_json(_PENDING_PROMISES_KEY, [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def save_pending_promises(self, promises: list[dict[str, Any]]) -> None:
        """整批覆盖持久化的跨重启约定快照。

        :param promises: 可 JSON 序列化的约定字典列表。

        :raises sqlite3.Error: meta 值写入或提交失败。
        :raises TypeError: 约定列表不可 JSON 序列化。

        副作用：
            覆盖 meta 表中的约定 JSON 值并提交事务。
        """
        self.write_json(_PENDING_PROMISES_KEY, promises)

    def read_json(self, key: str, fallback: Any) -> Any:
        """读取 meta 表中的 JSON 值，解析失败时返回调用方指定的回退值。

        :param key: meta 表键名。
        :param fallback: 键不存在或 JSON 无效时返回的值。
        :return: JSON 解码后的对象，或 `fallback`。
        副作用：只读 meta 表。
        """
        row = self._db.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        if not row:
            return fallback
        try:
            return json.loads(row[0])
        except Exception:
            return fallback

    def write_json(self, key: str, value: Any) -> None:
        """把值序列化为 JSON 并覆盖写入 meta 表。

        :param key: meta 表键名。
        :param value: 可 JSON 序列化的值。
        :return: 无返回值。
        :raises TypeError: 值不可 JSON 序列化。
        :raises sqlite3.Error: 写入或提交失败。
        副作用：插入/更新 meta 记录并提交事务。
        """
        self._db.execute(
            'INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)',
            (key, json.dumps(value, ensure_ascii=False))
        )
        self._db.commit()

    def user_spoke_after(self, stream_id: int, at: int) -> bool:
        """判断指定 stream 在时间点之后是否出现用户消息。

        :param stream_id: 目标 stream ID。
        :param at: 比较用的 Unix 毫秒时间戳，严格使用 `created_at > at`。
        :return: 存在匹配用户消息时返回 `True`。
        副作用：只读 messages 表。
        """
        row = self._db.execute(
            """SELECT 1 FROM messages
               WHERE stream_id = ? AND role = 'user' AND created_at > ? LIMIT 1""",
            (stream_id, at),
        ).fetchone()
        return row is not None
