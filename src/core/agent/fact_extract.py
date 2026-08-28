"""把一段已经发生的对话抽成结构化的人物事实，供长期记忆写入。

本模块是记忆写入的**独立一级**，不参与回复生成。它存在的理由来自真机实测：
让正在说话的对话模型顺手打 ``<memory>`` 标签，192 次调用产出 0 条，因为那个模型
既看不到 ``facts`` 表（无从判断「已经记过」），又背着协议里「通常一个都不写」的总闸。
形态照搬同一个代码库里已经跑通的 :func:`src.core.agent.summarize.summarize`——
回合之后的后台任务、独立模型任务槽、按阈值触发、失败只丢该批。

核心职责与对外接口：

- :func:`read_cursor` / :func:`advance_cursor`：抽取自己的进度游标，存在 ``meta``
  键值表里，与摘要的 ``episode_id`` 队列完全解耦。
- :func:`render_dialogue` / :func:`render_participants` / :func:`render_known_facts`：
  组装模型输入；「已经记住的」清单是本模块成立的关键，见 :func:`render_known_facts`。
- :func:`parse_extraction`：严格校验模型输出，非法即整批丢弃；同一次往返
  同时给出人物事实与知识候选。
- :func:`extract_facts`：一次模型往返。
- :func:`persist_facts`：按平台编号归属落库，走 ``MemoryStore.add_fact``。
- :func:`run_extraction`：上面几步的编排，调用方只需提供 store、provider 与在场者。

依赖：``src.core.memory.store``（读消息、写事实）、``src.core.agent.history``
（剥协议标签）、``src.core.prompts.registry``（``memory.extract`` 模板）、
``src.core.llm_models``（provider 与请求快照）、``src.core.observe``（观测事件）。
被 ``src.core.services.chat`` 在回合收尾处调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import json
import re
import sqlite3

from .history import strip_say_tags, strip_side_effect_tags
from .profile import mark_dirty as mark_profiles_dirty

from src.core.common.clock import now as current_time
from src.core.llm_models.protocol import LlmProvider
from src.core.llm_models.snapshot import bind_render_params
from src.core.memory.association import link_together
from src.core.memory.knowledge import add_knowledge
from src.core.memory.store import (
    FactInput,
    MemoryStore,
    StoredMessage,
    is_assistant_action_message,
)
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt, prompt_metadata

# 游标存在 meta 键值表里：抽取不新建表、不加列、不占迁移号（v13 归精力线、v14 归生活线）。
CURSOR_KEY = 'fact_extract_cursor'
# 「已经记住的」清单的总条数上限。超过这个量提示词开销显著上升，而模型对长清单的
# 遵守度反而下降；每人取前几条已经足够挡住重复。
KNOWN_FACT_LIMIT = 30
# 单个在场者取多少条既有事实进清单。群聊在场者可能十几个，逐人不设限会撑爆上限。
KNOWN_FACT_PER_PERSON = 6
# 模型未给出 kind 时的兜底类别，与 FactInput 的默认值一致。
DEFAULT_KIND = '未分类'
# 知识层的来源标识，与迁移进来的历史知识区分开，便于事后核对哪些是她自己学到的。
KNOWLEDGE_SOURCE = 'fact_extract'
# 对话正文短于此长度时不发起模型请求：抽不出东西，纯浪费一次往返。
MIN_DIALOGUE_CHARS = 40


@dataclass
class Participant:
    """表示一次抽取里的在场者，用于归属判定。

    :ivar external_id: 平台编号（QQ 号等）。群聊里昵称会重复也会改，编号是唯一稳定的锚。
    :ivar display_name: 展示名，只进提示词供模型辨认，不参与归属。
    :ivar person_id: 本地 ``persons.id``，落库时使用。
    """

    external_id: str
    display_name: str
    person_id: int


@dataclass
class ExtractedFact:
    """表示模型抽出的一条待写入事实。

    :ivar person_ref: 模型声明的归属对象，必须是输入名单里出现过的平台编号。
    :ivar kind: 事实类别，两到四个字。
    :ivar content: 脱离原对话也能读懂的一句话。
    """

    person_ref: str
    kind: str
    content: str


def read_cursor(store: MemoryStore, stream_id: int) -> int:
    """读取某个 stream 的抽取进度游标。

    :param store: 记忆存储实例。
    :param stream_id: 目标 stream ID。
    :return: 已抽取到的最后一条消息 ID；从未抽取过时返回 ``0``。
    副作用：只读 ``meta`` 表。
    """

    raw = store.read_json(CURSOR_KEY, {})
    if not isinstance(raw, dict):
        return 0
    value = raw.get(str(stream_id))
    return int(value) if isinstance(value, int) else 0


def advance_cursor(store: MemoryStore, stream_id: int, message_id: int) -> None:
    """把某个 stream 的抽取游标推进到指定消息。

    只在整批成功落库后调用；失败时保持不动，下次重跑同一批。

    :param store: 记忆存储实例。
    :param stream_id: 目标 stream ID。
    :param message_id: 本批最后一条消息的 ID。
    :return: 无返回值。
    副作用：写入 ``meta`` 表并提交事务。
    """

    raw = store.read_json(CURSOR_KEY, {})
    cursor: Dict[str, int] = dict(raw) if isinstance(raw, dict) else {}
    cursor[str(stream_id)] = int(message_id)
    store.write_json(CURSOR_KEY, cursor)


def render_participants(participants: Sequence[Participant]) -> str:
    """把在场者渲染成带平台编号的名单。

    :param participants: 本批对话的在场者。
    :return: 每行 ``[编号] 展示名`` 的文本；无在场者时返回空字符串。
    副作用：无。
    """

    return '\n'.join(f'  [{p.external_id}] {p.display_name}' for p in participants)


def render_known_facts(
    store: MemoryStore,
    participants: Sequence[Participant],
    now: Optional[int] = None,
) -> str:
    """渲染在场者已经被记住的事实清单。

    **这是本模块成立的关键。** 诊断记录里那条「已经记过的内容都不要写」之所以让
    对话模型彻底沉默，是因为它无从验证什么算已经记过；抽取是后台任务，可以先查表，
    于是同一条禁令从不可验证的自我审查变成了可逐条比对的清单。

    :param store: 记忆存储实例。
    :param participants: 本批对话的在场者。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: 每行一条既有事实的文本；没有任何既有事实时返回空字符串。
    副作用：只读 ``facts`` 表，不回补留存度——查表本身不该改写记忆权重。
    """

    now = now if now is not None else current_time()
    lines: List[str] = []
    for person in participants:
        for fact in store.top_facts(person.person_id, KNOWN_FACT_PER_PERSON, now):
            lines.append(f'  - {fact.content}')
            if len(lines) >= KNOWN_FACT_LIMIT:
                return '\n'.join(lines)
    return '\n'.join(lines)


def render_dialogue(
    messages: Sequence[StoredMessage],
    participants: Sequence[Participant],
    bot_name: str,
) -> str:
    """把消息批渲染成模型可读的逐行对话。

    助手动作伪消息只供聊天历史回看，在这里整条跳过；真实发言先剥副作用标签再剥
    ``<say>`` 外壳，避免把内部协议喂给抽取模型——它会把标签当成可以模仿的格式，
    输出里混进 XML 就无法按 JSON 解析。

    :param messages: 按 ID 正序排列的消息批。
    :param participants: 在场者，用于把 ``sender_person_id`` 还原成带编号的说话人。
    :param bot_name: Bot 展示名，用于标注她自己的发言。
    :return: 每行 ``说话人：内容`` 的文本；有效内容为空时返回空字符串。
    副作用：不修改输入消息。
    """

    by_person = {p.person_id: p for p in participants}
    lines: List[str] = []
    for message in messages:
        if message.role == 'assistant':
            if is_assistant_action_message(message.content):
                continue
            text = strip_say_tags(strip_side_effect_tags(message.content or ''))
            speaker = bot_name
        else:
            text = (message.content or '').strip()
            person = by_person.get(message.sender_person_id or -1)
            speaker = f'[{person.external_id}] {person.display_name}' if person else '某人'
        # 一条消息压成一行：她的多气泡回复经 strip_say_tags 后是换行分隔的，
        # 直接输出会产生没有说话人前缀的续行，抽取模型无从判断那句是谁说的。
        text = ' '.join(part for part in text.splitlines() if part.strip()).strip()
        if text:
            lines.append(f'{speaker}：{text}')
    return '\n'.join(lines)


@dataclass
class Extraction:
    """一次抽取的完整产出：人物事实与知识候选。

    两者由同一次模型往返产出，而不是各跑一次——同一段对话读两遍是纯粹的浪费，
    而且两次读的结果可能互相矛盾。

    :ivar facts: 关于具体某个人的稳定事实，写入 L2 ``facts``。
    :ivar knowledge: 与人无关的客观信息，写入 L3 ``knowledge``。
    """

    facts: List[ExtractedFact]
    knowledge: List[str]


def parse_extraction(raw: str) -> Optional[Extraction]:
    """从模型输出中提取并校验事实与知识候选。

    契约是一个对象而非裸数组：知识候选没有归属也没有类别，硬塞进事实数组只能靠
    判别字段区分，那会让「缺字段」既可能是知识也可能是坏数据，无法整批判废。

    :param raw: 可能带 Markdown 代码围栏或额外说明的模型输出。
    :return: 校验通过的产出（两个列表都空是合法结果，表示这批没什么可记的）；
        JSON 非法、顶层不是对象、或任一事实条目缺字段时返回 ``None`` 表示整批丢弃。
    副作用：不写存储，不抛出解析异常。
    """

    text = re.sub(r'```(?:json)?', '', raw or '', flags=re.IGNORECASE).strip()
    start = text.find('{')
    end = text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    # 两个键一个都没有时判废，而不是当成「这批没什么可记的」。
    # - 现象：旧契约的裸数组 `[{"person":..., "content":...}]` 会被上面的
    #   find('{') / rfind('}') 抓成其中的内层对象，从而被解析成一个合法的空产出。
    # - 后果：模型若退回旧格式，本批的事实会被静默丢弃，游标却照常推进——
    #   这段对话再也不会被重抽一次，且全程不报错。
    if 'facts' not in payload and 'knowledge' not in payload:
        return None
    raw_facts = payload.get('facts')
    raw_knowledge = payload.get('knowledge')
    # 两个键都允许缺省（等同空列表），但给了就必须是数组——给成别的类型说明模型
    # 没按契约输出，整批不可信。
    if raw_facts is None:
        raw_facts = []
    if raw_knowledge is None:
        raw_knowledge = []
    if not isinstance(raw_facts, list) or not isinstance(raw_knowledge, list):
        return None

    facts: List[ExtractedFact] = []
    for item in raw_facts:
        if not isinstance(item, dict):
            return None
        person = item.get('person')
        content = item.get('content')
        # 归属和正文缺一不可：没有归属的事实无处安放，没有正文的条目没有意义。
        # 这里返回 None 而不是跳过该条——模型没按契约输出时整批不可信。
        if not isinstance(person, str) or not person.strip():
            return None
        if not isinstance(content, str) or not content.strip():
            return None
        kind = item.get('kind')
        facts.append(ExtractedFact(
            person_ref=person.strip(),
            kind=kind.strip() if isinstance(kind, str) and kind.strip() else DEFAULT_KIND,
            content=content.strip(),
        ))

    knowledge: List[str] = []
    for item in raw_knowledge:
        # 知识条目只有正文。非字符串或空串**单条跳过**而不是整批丢弃：知识是旁路
        # 产物，不该因为它的一条脏数据牵连本批的事实写入。
        if isinstance(item, str) and item.strip():
            knowledge.append(item.strip())
    return Extraction(facts=facts, knowledge=knowledge)


async def extract_facts(
    provider: LlmProvider,
    *,
    bot_name: str,
    participants: Sequence[Participant],
    known_facts: str,
    dialogue: str,
    temperature: float,
    max_tokens: Optional[int],
) -> Optional[Extraction]:
    """请求模型从一段对话里抽出人物事实与知识候选。

    :param provider: 提供流式文本输出的模型客户端。
    :param bot_name: Bot 展示名，进系统提示词。
    :param participants: 在场者名单。
    :param known_facts: :func:`render_known_facts` 的产物，可为空字符串。
    :param dialogue: :func:`render_dialogue` 的产物。
    :param temperature: 采样温度。
    :param max_tokens: 输出上限；``None`` 表示由 provider 决定。
    :return: 抽出的事实列表；对话过短、模型调用失败或输出不合契约时返回 ``None``。
    副作用：发起一次流式模型请求并记录 ``llm_request`` 观测事件，不写数据库。
    :performance: 请求体长度与对话正文加既有事实清单成正比，网络耗时占主要成本。
    """

    if len(dialogue) < MIN_DIALOGUE_CHARS or not participants:
        return None
    render_params = {'memory.extract': {'bot_name': bot_name}}
    sections = [f'在场的人：\n{render_participants(participants)}']
    if known_facts:
        sections.append(f'你已经记住的（不要重复写这些）：\n{known_facts}')
    sections.append(f'对话：\n{dialogue}')
    request_messages = [
        {'role': 'system', 'content': get_prompt('memory.extract').render(bot_name=bot_name)},
        {'role': 'user', 'content': '\n\n'.join(sections)},
    ]
    raw = ''
    try:
        trace.emit(
            'llm_request',
            messages=request_messages,
            temperature=temperature,
            maxTokens=max_tokens,
            renderParams=render_params,
            **prompt_metadata('memory.extract', ('memory.extract',)),
        )
        bind_render_params(render_params)
        async for chunk in provider.stream(
            messages=request_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            if chunk.get('text'):
                raw += chunk['text']
    except Exception:
        # 抽取是旁路设施：模型故障不该让已经完成的回合受任何影响，整批丢弃即可。
        return None
    return parse_extraction(raw)


def persist_facts(
    store: MemoryStore,
    facts: Sequence[ExtractedFact],
    participants: Sequence[Participant],
    db: sqlite3.Connection,
    now: Optional[int] = None,
) -> List[int]:
    """按平台编号归属把事实写入长期记忆。

    去重完全交给 ``MemoryStore.add_fact``：它先按 ``exact_key`` 精确命中，未命中时
    取 FTS 候选逐个过 ``is_same_fact`` 的字符与 bigram 双阈值，命中即强化既有行。
    本函数不再叠任何一层判重——那会让同一件事算两遍。

    :param store: 记忆存储实例。
    :param facts: :func:`parse_extraction` 校验过的事实列表。
    :param participants: 在场者名单，用于把平台编号解析成 ``person_id``。
    :param db: 当前库连接，用于给写过新事实的人置画像脏位。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: 实际写入或强化的事实 ID 列表，顺序与输入一致；被丢弃的条目不占位。
    :raises sqlite3.Error: 写入失败时由 ``add_fact`` 抛出。
    副作用：写入 ``facts`` 与 ``facts_fts`` 并提交事务。
    """

    now = now if now is not None else current_time()
    by_external = {p.external_id: p for p in participants}
    written: List[int] = []
    touched: set[int] = set()
    for fact in facts:
        person = by_external.get(fact.person_ref)
        if person is None:
            # 归属不明整条丢弃，不猜。记错人比不记更糟：它会被反复召回，
            # 而且没有任何人会发现——不设兜底是刻意的。
            trace.emit('memory_fact_dropped', reason='unknown_person', personRef=fact.person_ref)
            continue
        fact_id = store.add_fact(person.person_id, FactInput(content=fact.content, kind=fact.kind), now)
        if fact_id:
            # 写入成功必须发事件：`<memory>` 标签那条旧写入路径连同它的 memory_fact
            # 事件一起删掉了，本处是这个事件此后唯一的生产者。少了它，控制台与
            # WebUI 都看不见事实写入，★W1-1「facts 新增并伴随写入事件」也无从验证。
            trace.emit(
                'memory_fact',
                factId=fact_id,
                personId=person.person_id,
                memoryKind=fact.kind,
                content=fact.content,
            )
            written.append(fact_id)
            touched.add(person.person_id)
    # 写过新事实的人，画像随之过期。置位在这里而不是在画像模块里反查，
    # 是因为「谁被写过」只有这一层知道；画像刷新是后台任务，只消费脏位。
    mark_profiles_dirty(db, sorted(touched), now)
    return written


def persist_knowledge(
    db: sqlite3.Connection,
    candidates: Sequence[str],
    now: Optional[int] = None,
) -> List[int]:
    """把与人无关的客观信息写入知识层（L3）。

    与事实抽取共用同一次模型往返，避免为知识再读一遍同样的对话。去重与建索引
    都由 :func:`~src.core.memory.knowledge.add_knowledge` 承担，本函数不再叠一层。

    :param db: 当前库连接；与 ``lookup_jargon`` 同惯例直接收连接，
        不从 ``MemoryStore`` 扒私有属性。
    :param candidates: :func:`parse_extraction` 校验过的知识正文列表。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: 新建或命中的知识行 ID 列表。
    :raises sqlite3.Error: 写入失败时由 ``add_knowledge`` 抛出。
    副作用：写入 ``knowledge`` 与 ``knowledge_fts`` 并提交事务。
    """

    now = now if now is not None else current_time()
    ids: List[int] = []
    for content in candidates:
        kid = add_knowledge(db, content, KNOWLEDGE_SOURCE, now)
        # 同一批里换个说法重复提到同一件事时 add_knowledge 会返回同一行 ID。
        # 去重后再计数，否则观察事件里的「数量」会大于库里实际新增的行数。
        if kid and kid not in ids:
            ids.append(kid)
    if ids:
        trace.emit('knowledge_learned', count=len(ids), source=KNOWLEDGE_SOURCE)
    return ids


async def run_extraction(
    store: MemoryStore,
    provider: LlmProvider,
    db: sqlite3.Connection,
    *,
    stream_id: int,
    participants: Sequence[Participant],
    bot_name: str,
    trigger_messages: int,
    batch_messages: int,
    temperature: float,
    max_tokens: Optional[int],
    now: Optional[int] = None,
) -> Optional[List[int]]:
    """检查触发条件并完成一次抽取。

    调用方（``ChatService``）在回合收尾处调用，**不要放进回复的关键路径**：
    它是后台任务，失败不阻塞任何回合。同一 stream 的并发去重由调用方负责，
    形态与 ``_maybe_summarize`` 的内存集合一致。

    :param store: 记忆存储实例。
    :param provider: 抽取任务的模型客户端。
    :param stream_id: 目标 stream ID。
    :param participants: 本 stream 的在场者，由调用方从人物注册表解析。
    :param bot_name: Bot 展示名。
    :param trigger_messages: 游标之后累积多少条消息才触发一次抽取。
    :param batch_messages: 单次交给模型的消息条数，必须小于 ``trigger_messages``。
    :param temperature: 采样温度。
    :param max_tokens: 输出上限。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: 写入的事实 ID 列表（可能为空列表，表示这批确实没什么可记的）；
        未达触发条件或整批被丢弃时返回 ``None``。
    :raises sqlite3.Error: 落库失败时由 ``add_fact`` 抛出。
    副作用：可能发起一次模型请求、写入 facts、推进游标，并发出观测事件。
    """

    now = now if now is not None else current_time()
    cursor = read_cursor(store, stream_id)
    if store.message_count_after(stream_id, cursor) < trigger_messages:
        return None
    batch = store.messages_after(stream_id, cursor, batch_messages)
    if not batch:
        return None
    extraction = await extract_facts(
        provider,
        bot_name=bot_name,
        participants=participants,
        known_facts=render_known_facts(store, participants, now),
        dialogue=render_dialogue(batch, participants, bot_name),
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if extraction is None:
        # 解析失败或模型故障：不推进游标，下次重跑同一批。宁可重复抽一次，
        # 也不要因为一次故障永久跳过这段对话。
        trace.emit('memory_extract_failed', streamId=stream_id, cursor=cursor)
        return None
    written = persist_facts(store, extraction.facts, participants, db, now)
    knowledge_ids = persist_knowledge(db, extraction.knowledge, now)
    # 同批产出的事实与知识描述的是同一段时间里发生的事，这是最强的一类关联，
    # 也是联想层两种建边时机中的第一种（另一种是「一起被召回并被采用」，在认知动作那侧）。
    linked = link_together(
        db,
        [('fact', fact_id) for fact_id in written] + [('knowledge', kid) for kid in knowledge_ids],
        now,
    )
    advance_cursor(store, stream_id, batch[-1].message_id)
    trace.emit(
        'memory_extract',
        streamId=stream_id,
        messageCount=len(batch),
        extracted=len(extraction.facts),
        written=written,
        knowledge=len(knowledge_ids),
        edges=linked,
    )
    return written
