"""认知动作的定义与内置实现。

认知动作是 ReAct 回环中的非终局工具：不产生用户可见产物，将检索结果渲染为
观察文本回灌给模型，由模型在下一轮决定终局动作。轮次预算与动作空间收窄分别
由 ``ConversationAgent`` 的循环与 ``action_protocol.available_actions`` 负责，
本模块不处理；按名分发的职责已并入工具注册表，本模块只保留检索实现。

内置动作：

- ``recall``：检索长期记忆（会话在场者的事实与该 stream 的情节）。
- ``inspect``：检索本 stream 水位之前的聊天原文。
- ``consult``：检索知识层（knowledge）。知识不进入每轮上下文组装，仅在
  主动查询时出现。

依赖：``src.core.memory.store``、``src.core.memory.knowledge`` 的只读检索接口与
``src.core.platform_io.types``；三个实现由 ``src.core.services.chat`` 包装成
工具执行器绑定进注册表，不反向依赖聊天服务或模型层。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Protocol, Sequence

import sqlite3

from src.core.common.clock import now as current_time
from src.core.memory.association import (
    HOPS, ShortTermActivation, SpreadHit, link_together, node_id, spread,
)
from src.core.memory.knowledge import search_knowledge, touch_knowledge
from src.core.memory.store import MemoryStore, StoredMessage
from src.core.observe import events as trace
from src.core.platform_io.types import StreamKind

# 回灌给模型的观察正文上限。观察会占用本回合上下文预算，且每多一轮 ReAct 就再占一次；
# 超出上限只截断观察，不截断 Bot 原本的历史。
OBSERVATION_MAX_CHARS = 600
# 写入行动事件账本的观察摘要上限；账本仅反映查询内容与命中情况。
OBSERVATION_EVENT_MAX_CHARS = 300
# 单条检索结果的展示上限。一条超长消息不应挤掉其余命中项。
_ITEM_MAX_CHARS = 80

# (人物 ID, stream ID) -> 该 stream 内的显示名。群聊历史正文不存储说话人前缀，
# 渲染检索结果时必须实时查询，否则观察结果中会出现无归属的发言。
SpeakerNamer = Callable[[int, int], str]


def _clip(text: str, limit: int) -> str:
    """按字符上限截断文本并显式标注截断，避免模型把残句当完整原文。

    :param text: 原始文本。
    :param limit: 字符上限；必须大于 0。
    :return: 未超限时原样返回，超限时返回截断后加省略标记的文本。
    """
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return f'{stripped[:limit]}…（已截断）'


@dataclass(frozen=True)
class CognitiveScope:
    """一个回合内所有认知动作共享的检索范围。

    范围在回合开始时定死、不随轮次变化：Bot 这一回合能查到的东西是固定的，
    否则「回合固定快照」这条性质会被多轮检索悄悄破坏。

    :ivar stream_id: 当前会话 ID；检索不跨会话，与既有隐私硬隔离同一条边界。
    :ivar person_ids: 事实检索覆盖的人物范围，通常是本回合上下文里的在场者。
    """

    stream_id: int
    person_ids: tuple[int, ...]


@dataclass(frozen=True)
class CognitiveRequest:
    """一次认知动作的执行请求。

    :ivar action: 动作名，必须是执行器已注册的名字。
    :ivar query: 模型在动作头里写的检索词，已确认非空。
    :ivar stream_id: 当前会话 ID；检索范围不跨会话，隐私边界与既有硬隔离一致。
    :ivar stream_kind: 会话类型；决定检索结果要不要带说话人显示名。
    :ivar person_ids: 检索事实时的人物范围，通常是本回合上下文里的在场者。
    :ivar message_watermark: 本回合消息水位；历史检索只看水位之前的消息，
        使认知轮看到的东西不会随批次外新消息漂移。
    """

    action: str
    query: str
    stream_id: int
    stream_kind: StreamKind
    person_ids: tuple[int, ...]
    message_watermark: int


@dataclass(frozen=True)
class CognitiveObservation:
    """一次认知动作的执行结果。

    :ivar text: 回灌给模型的观察正文；无命中时返回明确的「没找到」文本而非空串，
        供模型区分「想不起来」与需要编造的情况。
    :ivar hit_count: 命中条目数，供事件账本与后续标定使用。
    """

    text: str
    hit_count: int

    def __post_init__(self) -> None:
        """拒绝空观察：无命中必须由动作显式声明。"""
        if not self.text.strip():
            raise ValueError('认知动作的观察正文不能为空')
        if self.hit_count < 0:
            raise ValueError('认知动作的命中条目数不能为负')


class CognitiveAction(Protocol):
    """一个认知动作需要满足的窄协议。"""

    name: str

    async def execute(self, request: CognitiveRequest) -> CognitiveObservation:
        """执行检索并返回可直接回灌给模型的观察。"""
        ...


class RecallAction:
    """检索长期记忆：会话在场者的事实与该 stream 的情节。

    事实与情节按需检索，不逐轮无条件注入系统提示词；常驻注入仅保留最小集。
    """

    name = 'recall'

    def __init__(
        self,
        store: MemoryStore,
        speaker_name: SpeakerNamer,
        db: sqlite3.Connection,
        *,
        fact_limit: int = 5,
        episode_limit: int = 3,
        private_in_group: bool = False,
    ) -> None:
        """保存记忆检索依赖与每类结果的条数上限。

        :param store: 记忆存储；只使用其只读检索接口。
        :param speaker_name: 人物显示名解析函数，用于说明事实归属于谁。
        :param db: 当前库连接，供联想层读写边；与 ``ConsultAction`` 同惯例直接收连接。
        :param fact_limit: 单次返回的事实条数上限，必须大于 0。
        :param episode_limit: 单次返回的情节条数上限，必须大于 0。
        :param private_in_group: ``conversation.private_facts_in_group`` 的当前值；
            事实可见性由读取场合决定，见 ``memory/scope.py``。
        :raises ValueError: 任一上限小于 1。
        """
        if fact_limit < 1 or episode_limit < 1:
            raise ValueError('记忆检索条数上限必须大于 0')
        self._store = store
        self._speaker_name = speaker_name
        self._db = db
        self._fact_limit = fact_limit
        self._episode_limit = episode_limit
        self._private_in_group = private_in_group
        # 短期激活留在动作实例上，作用范围因此仅限进程内：进程重启后重新构造，残留自动消失。
        self._activation = ShortTermActivation()

    def _describe_node(self, hit: SpreadHit, stream_id: int) -> str:
        """把一条扩散命中渲染成一句「顺带想起」。

        :param hit: 扩散结果。
        :param stream_id: 当前会话 ID，用于解析事实归属的显示名。
        :return: 一句可直接进观察文本的描述；指向的记忆已不存在时返回空串。
        :raises sqlite3.Error: 查询失败。
        副作用：只读。
        """
        if hit.ref_kind == 'fact':
            row = self._db.execute(
                'SELECT person_id, content FROM facts WHERE id = ?', (hit.ref_id,)
            ).fetchone()
            if row is None:
                return ''
            who = self._speaker_name(int(row[0]), stream_id)
            return f'还想到关于{who}：{_clip(str(row[1]), _ITEM_MAX_CHARS)}'
        if hit.ref_kind == 'episode':
            row = self._db.execute(
                'SELECT summary FROM episodes WHERE id = ?', (hit.ref_id,)
            ).fetchone()
            return f'还想到你们聊过：{_clip(str(row[0]), _ITEM_MAX_CHARS)}' if row else ''
        row = self._db.execute(
            'SELECT content FROM knowledge WHERE id = ?', (hit.ref_id,)
        ).fetchone()
        return f'还想到：{_clip(str(row[0]), _ITEM_MAX_CHARS)}' if row else ''

    async def execute(self, request: CognitiveRequest) -> CognitiveObservation:
        """按检索词召回事实与情节并渲染为观察文本。

        :param request: 已校验的认知动作请求。
        :return: 含命中条目的观察；两类都为空时返回明确的「没想起来」。
        :raises sqlite3.Error: 底层检索失败时原样上抛，由 Agent 记为失败状态。
        副作用：只读记忆库；不回补事实强度，检索本身不应改写遗忘曲线。
        """
        facts = self._store.recall_facts_in_scope(
            request.person_ids, request.query, limit=self._fact_limit,
            stream_kind=request.stream_kind,
            private_in_group=self._private_in_group,
        )
        episodes = self._store.recall_episodes(
            request.stream_id, request.query, limit=self._episode_limit,
        )
        if not facts and not episodes:
            return CognitiveObservation(
                text=f'你想了一下，没有想起任何和「{request.query}」有关的事。',
                hit_count=0,
            )
        lines = [f'关于「{request.query}」，你想起这些：']
        for fact in facts:
            who = self._speaker_name(fact.person_id, request.stream_id)
            lines.append(f'- 你记得关于{who}：{_clip(fact.content, _ITEM_MAX_CHARS)}')
        for episode in episodes:
            lines.append(f'- 你们聊过：{_clip(episode.summary, _ITEM_MAX_CHARS)}')

        # 第二步：从命中的这些出发沿边扩散，把未被查询但被关联出来的内容也纳入观察。
        # 与种子分开成段：种子是 Bot 记得的内容，扩散结果是关联引出的内容；
        # 混在一起时模型会把联想当作确凿记忆复述。
        seeds = (
            [('fact', fact.id, fact.score) for fact in facts]
            + [('episode', episode.id, episode.score) for episode in episodes]
        )
        now = current_time()
        spread_hits = spread(self._db, seeds, now, activation=self._activation)
        if spread_hits:
            lines.append('顺带想起来的：')
            for hit in spread_hits:
                text = self._describe_node(hit, request.stream_id)
                if text:
                    lines.append(f'- {text}')

        adopted = [(kind, ref) for kind, ref, _ in seeds]
        adopted += [(hit.ref_kind, hit.ref_id) for hit in spread_hits]
        # 只有真正进了这段观察文本的才加强边——被检索到不等于被用到，
        # 这个区分是边质量的全部来源。
        link_together(self._db, adopted, now)
        self._activation.touch(
            [node_id(self._db, kind, ref) for kind, ref in adopted], now,
        )
        trace.emit(
            'memory_spread',
            query=request.query,
            seeds=len(seeds),
            spread=len(spread_hits),
            hops=HOPS,
        )
        return CognitiveObservation(
            text='\n'.join(lines),
            hit_count=len(facts) + len(episodes) + len(spread_hits),
        )


class InspectAction:
    """检索本 stream 水位之前的聊天原文。

    工作记忆窗口之外的消息对本轮不可见，本动作提供对该范围的读取。

    检索结果不进入 selectable_message_ids：可选集约束回复投递目标，检索仅扩展
    理解范围；二者分离以保持回合快照不变。
    """

    name = 'inspect'

    def __init__(
        self,
        store: MemoryStore,
        speaker_name: SpeakerNamer,
        *,
        limit: int = 6,
    ) -> None:
        """保存消息检索依赖与返回条数上限。

        :param store: 记忆存储；只使用其只读消息检索接口。
        :param speaker_name: 人物显示名解析函数，群聊结果按此加说话人前缀。
        :param limit: 单次返回的消息条数上限，必须大于 0。
        :raises ValueError: 上限小于 1。
        """
        if limit < 1:
            raise ValueError('历史消息检索条数上限必须大于 0')
        self._store = store
        self._speaker_name = speaker_name
        self._limit = limit

    async def execute(self, request: CognitiveRequest) -> CognitiveObservation:
        """按检索词翻找水位之前的历史消息并渲染为观察文本。

        :param request: 已校验的认知动作请求。
        :return: 按时间正序渲染的命中消息；无命中时返回明确的「没找到」。
        :raises sqlite3.Error: 底层检索失败时原样上抛，由 Agent 记为失败状态。
        副作用：只读消息表。
        """
        messages = self._store.search_messages(
            request.stream_id,
            request.query,
            before_id=request.message_watermark,
            limit=self._limit,
        )
        if not messages:
            return CognitiveObservation(
                text=(
                    f'你翻了翻更早的聊天记录，没有找到和「{request.query}」有关的内容。'
                ),
                hit_count=0,
            )
        lines = [f'更早的聊天记录里和「{request.query}」有关的几条（按时间正序）：']
        for message in messages:
            lines.append(f'- {self._label(message, request)}')
        return CognitiveObservation(text='\n'.join(lines), hit_count=len(messages))

    def _label(self, message: StoredMessage, request: CognitiveRequest) -> str:
        """把一条历史消息渲染成「说话人：正文」。

        :param message: 记忆库返回的消息记录。
        :param request: 当前请求，提供 stream 维度与会话类型。
        :return: 群聊带说话人显示名、私聊只给正文的单行文本。
        :raises RuntimeError: 群聊 user 消息缺少发送者人物 ID。
        """
        content = _clip(message.content, _ITEM_MAX_CHARS)
        if message.role == 'assistant':
            return f'你说：{content}'
        if request.stream_kind != 'group':
            return f'对方说：{content}'
        if message.sender_person_id is None:
            raise RuntimeError('群聊历史消息缺少 sender_person_id')
        return f'{self._speaker_name(message.sender_person_id, request.stream_id)}：{content}'


class ConsultAction:
    """检索知识层（knowledge）：概念、定义与事实性资料。

    与 ``recall`` 的分工：recall 检索有遗忘曲线的记忆，consult 检索无衰减的知识。
    命中经 ``touch_knowledge`` 记录计数，供检索调优，不参与打分。
    """

    name = 'consult'

    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        embed_query: Callable[[str], Awaitable[bytes | None]] | None = None,
        limit: int = 5,
    ) -> None:
        """保存知识检索依赖与返回条数上限。

        :param db: 当前库连接。知识检索直接走 ``memory.knowledge`` 而不经
            ``MemoryStore``——那里管的是「Bot 的记忆」的衰减曲线，知识没有曲线。
        :param embed_query: 可选的查询向量回调（``VectorService.embed_query``
            口径：服务禁用或失败时返回 ``None``）；为 ``None`` 时只用 BM25。
        :param limit: 单次返回的知识条数上限，必须大于 0。
        :raises ValueError: 上限小于 1。
        """
        if limit < 1:
            raise ValueError('知识检索条数上限必须大于 0')
        self._db = db
        self._embed_query = embed_query
        self._limit = limit

    async def execute(self, request: CognitiveRequest) -> CognitiveObservation:
        """按检索词查知识并渲染为观察文本。

        :param request: 已校验的认知动作请求。
        :return: 含命中条目的观察；无命中时返回明确的「没找到」。
        :raises sqlite3.Error: 底层检索失败时原样上抛，由 Agent 记为失败状态。
        副作用：只读知识库；向量服务失败时退回 BM25（与 recall 的向量缺失口径
            一致）；对实际展示的命中记 hit_count / last_hit_at 并提交事务。
        """
        query_embedding: bytes | None = None
        if self._embed_query is not None:
            try:
                query_embedding = await self._embed_query(request.query)
            except Exception:
                # 向量服务故障不阻断检索：退回纯 BM25，与 embed.py 的既有口径一致。
                query_embedding = None
        hits = search_knowledge(
            self._db, request.query, self._limit, query_embedding=query_embedding,
        )
        if not hits:
            return CognitiveObservation(
                text=f'你查了查自己知道的东西，没有找到和「{request.query}」有关的知识。',
                hit_count=0,
            )
        lines = [f'关于「{request.query}」，你查到这些知识：']
        for hit in hits:
            lines.append(f'- {_clip(hit.content, _ITEM_MAX_CHARS)}')
        touch_knowledge(self._db, [hit.id for hit in hits], current_time())
        return CognitiveObservation(text='\n'.join(lines), hit_count=len(hits))
