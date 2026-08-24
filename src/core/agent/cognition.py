"""认知动作的定义、三个内置实现与窄执行器。

认知动作是 ReAct 回环里的非终局动作：它们不产生任何用户可见产物，只把检索结果
渲染成一段观察文本回灌给模型，由模型在下一轮决定终局动作。本模块负责「查什么、
怎么查、查到的东西怎么说给她听」，不负责轮次预算与动作空间收窄——那两件事分别由
``ConversationAgent`` 的循环和 ``action_protocol.available_actions`` 表达。

三个内置动作：

- ``recall``：检索长期记忆（会话在场者的事实 + 该 stream 的情节），打的是
  「工作记忆窗口之外一片空白」这个缺口。
- ``inspect``：检索本 stream 水位之前的聊天原文，打的是「她想接的东西不在视野里」
  这个缺口。
- ``consult``：检索她知道的知识（knowledge 层），打的是「对方提到的东西她不懂」
  这个缺口。知识不进每轮组装——两万余条里绝大多数与当前对话无关——只在她
  主动查时出现。

**这里刻意不叫 Registry，也不做插件挂载点或可见性标签。** 项目至今只有三个真实
认知动作，先建注册表等于用想象中的工具定形状；等媒体资源动作也落地之后，再从
四个真实动作归纳统一协议。

依赖：``src.core.memory.store`` 的只读检索接口、``src.core.memory.knowledge``
的知识检索接口、``src.core.platform_io.types`` 的 ``StreamKind``；被
``src.core.services.chat`` 组装并透传给 ``ConversationAgent``，
不反向依赖聊天服务或模型层。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Protocol, Sequence

import sqlite3

from src.core.common.clock import now as current_time
from src.core.memory.knowledge import search_knowledge, touch_knowledge
from src.core.memory.store import MemoryStore, StoredMessage
from src.core.platform_io.types import StreamKind

# 回灌给模型的观察正文上限。观察会占用本回合上下文预算，且每多一轮 ReAct 就再占一次；
# 超出上限只截断观察，不截断她原本的历史。
OBSERVATION_MAX_CHARS = 600
# 写进行动事件账本的观察摘要上限。账本要能回答「她查了什么、查到没有」，不需要全文。
OBSERVATION_EVENT_MAX_CHARS = 300
# 单条检索结果的展示上限。一条超长消息不应挤掉其余命中项。
_ITEM_MAX_CHARS = 80

# (人物 ID, stream ID) -> 该 stream 内的显示名。群聊历史正文不落库说话人前缀，
# 渲染检索结果时必须现查，否则观察里会出现一堆无主发言。
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

    范围在回合开始时定死、不随轮次变化：她这一回合能查到的东西是固定的，
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

    :ivar text: 回灌给模型的观察正文；**无命中时也必须是明确的「没找到」而不是
        空串**——「查过了但没有」是真实信息，会决定她该说「我想不起来了」还是硬编。
    :ivar hit_count: 命中条目数，供事件账本与后续标定使用。
    """

    text: str
    hit_count: int

    def __post_init__(self) -> None:
        """拒绝空观察：无命中必须由动作自己写明，不允许留给调用方猜。"""
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

    对应「她想起来了」而不是「她随时全知」：事实与情节此前是每轮无条件灌进系统
    提示词的，那样既贵又不像人；改为按需检索之后，常驻注入只留最小集，深处的东西
    要她自己去想。
    """

    name = 'recall'

    def __init__(
        self,
        store: MemoryStore,
        speaker_name: SpeakerNamer,
        *,
        fact_limit: int = 5,
        episode_limit: int = 3,
    ) -> None:
        """保存记忆检索依赖与每类结果的条数上限。

        :param store: 记忆存储；只使用其只读检索接口。
        :param speaker_name: 人物显示名解析函数，用于说明事实归属于谁。
        :param fact_limit: 单次返回的事实条数上限，必须大于 0。
        :param episode_limit: 单次返回的情节条数上限，必须大于 0。
        :raises ValueError: 任一上限小于 1。
        """
        if fact_limit < 1 or episode_limit < 1:
            raise ValueError('记忆检索条数上限必须大于 0')
        self._store = store
        self._speaker_name = speaker_name
        self._fact_limit = fact_limit
        self._episode_limit = episode_limit

    async def execute(self, request: CognitiveRequest) -> CognitiveObservation:
        """按检索词召回事实与情节并渲染为观察文本。

        :param request: 已校验的认知动作请求。
        :return: 含命中条目的观察；两类都为空时返回明确的「没想起来」。
        :raises sqlite3.Error: 底层检索失败时原样上抛，由 Agent 记为失败状态。
        副作用：只读记忆库；**不回补事实强度**，检索本身不应改写遗忘曲线。
        """
        facts = self._store.recall_facts_in_scope(
            request.person_ids, request.query, limit=self._fact_limit,
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
        return CognitiveObservation(
            text='\n'.join(lines),
            hit_count=len(facts) + len(episodes),
        )


class InspectAction:
    """检索本 stream 水位之前的聊天原文。

    工作记忆窗口只保留最近若干条，窗口之外她看不见。实测里模型多次想接一条不在
    可选清单里的消息（记为 illegal_action、用户侧表现为她不回话），本动作让她能
    先把话头看清楚再决定接谁。

    检索结果**不进入 selectable_message_ids**：水位与可选集约束的是「回复投递给谁」，
    不是「理解范围」；让检索反过来扩可选集会让回合快照不再固定。
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
    """检索她知道的知识（knowledge 层）：概念、定义、事实性资料。

    与 ``recall`` 的分工：recall 翻的是「她记得的事」（事实与情节，有遗忘曲线），
    consult 查的是「她知道的东西」（知识，没有衰减）。知识不进入每轮组装，
    只在她主动 consult 时出现；命中经 ``touch_knowledge`` 落计数，供后续
    检索调优，不参与打分。
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
            ``MemoryStore``——那里管的是「她的记忆」的衰减曲线，知识没有曲线。
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


class CognitiveExecutor:
    """按动作名分发认知动作，并统一施加观察长度上限。

    只做分发和截断两件事。可用性判断（哪个动作这一轮能选）在
    ``available_actions``，轮次预算在 ``ConversationAgent`` 的循环里，
    本类不重复表达任何一处。
    """

    def __init__(self, actions: Sequence[CognitiveAction]) -> None:
        """按动作名建立分发表。

        :param actions: 已构造的认知动作序列。
        :raises ValueError: 动作序列为空或存在重名。
        """
        if not actions:
            raise ValueError('认知执行器至少需要一个动作')
        table: Dict[str, CognitiveAction] = {}
        for action in actions:
            if action.name in table:
                raise ValueError(f'认知动作重名：{action.name}')
            table[action.name] = action
        self._actions = table

    @property
    def action_names(self) -> tuple[str, ...]:
        """返回已注册的动作名，按注册顺序排列。"""
        return tuple(self._actions)

    async def execute(self, request: CognitiveRequest) -> CognitiveObservation:
        """执行一次认知动作并按上限截断观察正文。

        :param request: 待执行的认知动作请求。
        :return: 正文已按 ``OBSERVATION_MAX_CHARS`` 截断的观察。
        :raises KeyError: 动作名未注册；这代表动作空间与执行器不同步，
            属于装配错误而非模型错误，不应被当作协议失败吞掉。
        """
        action = self._actions[request.action]
        observation = await action.execute(request)
        clipped = _clip(observation.text, OBSERVATION_MAX_CHARS)
        if clipped == observation.text:
            return observation
        return CognitiveObservation(text=clipped, hit_count=observation.hit_count)
