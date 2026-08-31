"""从已经发生的对话里学习「什么情境下怎么说话」，并负责词表的淘汰。

运行时此前不存在表达词表的写入路径（全部条目来自一次性迁移）。本模块补充学习侧，
形态与 :func:`src.core.agent.fact_extract.run_extraction` 一致：回合后的后台任务、
独立游标、按阈值触发、失败仅丢弃该批。

语料口径：

- 游标与处理对账覆盖整批消息，但送入表达学习模型的语料只保留非助手发言。
  助手正文在代码层先被移除，不依赖提示词要求模型忽略；情境只能从群友前后
  发言中提炼，信息不足就不学。
- style 必须锚定群友说过的说法，不得学习 Bot 自身发言。
  - 现象：2026-08-28 表达库积累 9 条几乎同义的「……也没用」，全部是 Bot 本人
    08-17 至 08-21 原话的逐字转录；同期该句式在 Bot 发言中的占比从 0 升至 7.3%。
  - 原因：学习自身发言构成自举闭环：Bot 的发言被记录后注入提示词，再次影响
    发言，词表缺少外部输入，既有倾向被持续放大。
  - 后果：口癖被持续强化，且以「表达习惯」形式从隐性模仿变为显式指令。
  来源隔离阻断该闭环；注入一致性由复核闸门保障，每条须经人工确认才进入
  候选池（见 ``agent/expression.py`` 的 ``fetch_expression_pool``）。
- 本批除 Bot 之外无人发言时直接判无可学内容，游标照常推进：重跑同一批也
  不会产生新的群友发言。

核心职责与对外接口：

- :func:`read_cursor` / :func:`advance_cursor`：学习自己的进度游标，存在
  ``meta`` 键值表里。它是 ``messages_after`` 的第三个独立消费者（摘要用
  ``episode_id`` 队列、事实抽取用自己的游标），共用判据会让消费者相互消费对方的输入且不报错。
- :func:`strip_style_prefix` / :func:`validate_pairs`：落库前的前缀剥离与
  合法性检查。旧迁移存量 42% 的 style 为「使用反问句加强语气」这类指令句式，
  源于旧提示词示例；新学条目剥离同类前缀（口径与
  ``scripts/fix_expression_style_prefix.py`` 同源）。
- :func:`parse_learning`：严格校验模型输出，结构非法即整批丢弃。
- :func:`persist_pairs`：去重完全交给 ``UNIQUE(situation, style, stream_id)``
  + ``INSERT OR IGNORE``；字面不同但语义重复的条目会堆积，由淘汰兜住，
  不引入相似度模型。
- :func:`eliminate_stale`：确定性淘汰，与学习挂在同一次调用里，不另起后台任务。
- :func:`run_learning`：上面几步的编排，调用方只提供 store、provider 与批次参数。

淘汰规则为确定性规则，可解释，不用模型判断删哪条：

- 只淘汰本模块学来的行（``source = LEARN_SOURCE``）；迁移存量不自动删除，
  清理由人工在 WebUI 中进行。不得将存量纳入「未被选中即淘汰」：存量 3360 条
  分属两个会话，其中会话 5 的行全部未被本机选中过（2026-08-26 实测 1752 条、
  ``last_used_at`` 非空 0 条），纳入后该会话候选池归零，低于
  ``agent/expression.py`` 的 ``MIN_POOL_CANDIDATES``，表达选择停摆。
- 「从未在本机被选中」的判据是 ``last_used_at IS NULL``，不是 ``use_count = 0``：
  本模块学到的行两列同时回写（选中才同时加一、打时间戳），两判据等价；存量
  的 ``use_count`` 是旧部署的历史值，不构成本机使用证据。取 ``last_used_at``
  是其语义不依赖数据来源。
- ``checked = 1``（人工确认）的行永不淘汰：人工确认的条目不得被后台任务删除。
  ``checked = -1``（人工驳回）同样不删：驳回是已判定的标记，删除后学习器可能
  再次提出同一内容；其失效由候选池 SQL 排除实现，不依赖删除。
- 复核为放行闸门（只有 ``checked = 1`` 进候选池）之后，未复核的行不可能被
  选中，``last_used_at`` 恒为 NULL，「从未被选中」不构成价值判断，而是必然
  状态。:data:`RETENTION_MS` 的实际语义是复核期限：超期未复核的建议被清除，
  由学习器在遇到同样说法时重新提出。
- 总量上限只约束本模块学来的行，与存量条数无关；否则按全表计算时上限退化为
  每轮删除新学条目。

依赖：``src.core.memory.store``（读消息与游标）、``src.core.agent.sub_agent``
（统一模型执行器）、``src.core.prompts.registry``（``expression.learn`` 模板）、
``src.core.observe``（观测事件）。被 ``src.core.services.chat`` 在回合收尾处调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import json
import re
import sqlite3

from .fact_extract import Participant, render_dialogue
from .sub_agent import SubAgentCall, run_sub_agent

from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.llm_models.protocol import LlmProvider
from src.core.memory.store import MemoryStore, StoredMessage
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt, prompt_metadata

logger = get_logger(__name__)

# 游标存在 meta 键值表里，与事实抽取的 fact_extract_cursor、摘要的 episode_id
# 队列完全解耦；不新建表、不加列、不占迁移号。
CURSOR_KEY = 'expression_learn_cursor'
# 本机学习的来源标识，与迁移存量（历史迁移:M-2）便于按 source 区分。
LEARN_SOURCE = '本机学习'

# 触发阈值与批大小，单位都是「消息条数」。表达信号比人物事实稀疏：批次 16 条
# 约覆盖 6~10 个来回，足以呈现说法所在的情境；阈值 24 限制活跃会话的学习频次。
# 与事实抽取的 32/12 取值不同：两个消费者各用各的游标，取值无联动。
TRIGGER_MESSAGES = 24
BATCH_MESSAGES = 16

# 校验上限，单位字符。真机存量 style 最长 31、situation 最长 39（2026-08-26
# 实测），上限取约 1.5 倍余量；注入模板会把 style 原文拼进系统提示词，超长
# 条目占用提示词预算且多为异常输出。
MAX_SITUATION_CHARS = 60
MAX_STYLE_CHARS = 60
# 单批最多落库条数。提示词要求最多 5 条，硬上限放宽到 8 条，限制超量输出。
MAX_PAIRS_PER_BATCH = 8

# 淘汰保留期，单位毫秒（30 天）。候选池每轮只抽 10 条（agent/expression.py），
# 本模块学来的说法一个月未被选中，视为在该会话无适配场景。存量不适用：
# 其入库时间是迁移日期，与学习后使用情况无关。
RETENTION_MS = 30 * 24 * 60 * 60 * 1000
# 本模块学来的行的总量上限（条）。口径是「学来的行」而不是全表：存量 3360 条，
# 按全表计算时上限退化为每轮删除新学条目。取池按会话全取后内存抽样，该上限
# 同时约束扫描成本与长尾噪声；2000 已远超抽样能覆盖的量级。
MAX_LEARNED_ROWS = 2000

# 对话正文短于此长度时不发起模型请求：整批都是占位符或空行，学不出东西。
MIN_DIALOGUE_CHARS = 40


@dataclass(frozen=True)
class ExpressionPair:
    """一条待落库的「情境 → 说法」二元组。

    :ivar situation: 适用情境描述，注入提示词时只展示这一字段供选择模型判断。
    :ivar style: 该情境下的说法句式，选中之后才作为载荷拼进注入文本。
    """

    situation: str
    style: str


@dataclass(frozen=True)
class LearnReport:
    """一次学习调用的完整对账。

    :ivar message_count: 本批交给模型的消息条数。
    :ivar candidates: 模型按契约返回的原始二元组条数。
    :ivar learned: 实际落库的条数（``INSERT OR IGNORE`` 去重后）。
    :ivar discarded: 校验丢弃的条数（前缀剥完为空、超长、超批上限）。
    :ivar eliminated: 本次淘汰删除的条数。
    :ivar remaining: 淘汰后全表剩余条数。
    """

    message_count: int
    candidates: int
    learned: int
    discarded: int
    eliminated: int
    remaining: int


def render_learning_dialogue(
    messages: Sequence[StoredMessage],
    participants: Sequence[Participant],
    bot_name: str,
) -> str:
    """渲染表达学习专用语料，确保助手台词不进入模型输入。

    表达学习语料不得包含助手正文：模型可能把 Bot 的现有口癖重新提炼为表达
    建议，形成自我强化闭环。因此先在代码层移除全部 ``assistant`` 消息，再
    复用公共渲染器还原群友身份与压平多行正文。

    :param messages: 按 ID 正序排列的完整学习批次。
    :param participants: 本批在场者，用于还原群友的展示名与平台编号。
    :param bot_name: 传给公共渲染器的 Bot 名；助手消息已被移除，不会出现在结果中。
    :return: 只含非助手发言的逐行语料；没有可学习发言时返回空字符串。
    副作用：不修改输入消息。
    """

    peer_messages = [
        message
        for message in messages
        if message.role == 'user' and (message.content or '').strip()
    ]
    return render_dialogue(peer_messages, participants, bot_name)


def read_cursor(store: MemoryStore, stream_id: int) -> int:
    """读取某个 stream 的学习进度游标。

    :param store: 记忆存储实例。
    :param stream_id: 目标 stream ID。
    :return: 已学习到的最后一条消息 ID；从未学习过时返回 ``0``。
    副作用：只读 ``meta`` 表。
    """

    raw = store.read_json(CURSOR_KEY, {})
    if not isinstance(raw, dict):
        return 0
    value = raw.get(str(stream_id))
    return int(value) if isinstance(value, int) else 0


def advance_cursor(store: MemoryStore, stream_id: int, message_id: int) -> None:
    """把某个 stream 的学习游标推进到指定消息。

    只在整批处理完毕（落库与淘汰都完成）后调用；失败时保持不动，下次重跑同一批。

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


# 旧学习提示词带出来的前导词。剥离按「最长在前」逐轮进行：「使用」先于「用」匹配，
# 剥完一轮再检查一轮，挡住「使用用……」这类叠写。
_LEADING_PREFIXES = ('使用', '采用', '用')


def strip_style_prefix(style: str) -> str:
    """剥掉 style 开头的前导词并裁掉两端空白。

    :param style: 模型给出的原始说法文本。
    :return: 剥净后的说法；剥完为空时返回空字符串，由调用方整条丢弃。
    副作用：无。
    """

    text = style.strip()
    changed = True
    while changed and text:
        changed = False
        for prefix in _LEADING_PREFIXES:
            # 整条恰好是前导词时同样剥，剥完为空由调用方整条丢弃。
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                changed = True
                break
    return text


def parse_learning(raw: str) -> Optional[List[ExpressionPair]]:
    """从模型输出中解析并校验「情境 → 说法」二元组列表。

    契约是一个对象而非裸数组，与事实抽取同一条纪律：裸数组无法区分「模型没按
    契约输出」与「这批没什么可学的」，前者必须整批判废。

    :param raw: 可能带 Markdown 代码围栏或额外说明的模型输出。
    :return: 校验通过的二元组列表（空列表是合法结果，表示这批没什么可学的）；
        JSON 非法、顶层不是对象、缺 ``expressions`` 键、或任一条目缺字段时
        返回 ``None`` 表示整批丢弃。
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
    raw_pairs = payload.get('expressions')
    # 键缺失或不是数组都说明模型没按契约输出，整批不可信——判废而不是当成空结果，
    # 否则游标照常推进，这段对话再也不会被重学一次，且全程不报错。
    if not isinstance(raw_pairs, list):
        return None
    pairs: List[ExpressionPair] = []
    for item in raw_pairs:
        if not isinstance(item, dict):
            return None
        situation = item.get('situation')
        style = item.get('style')
        # 情境和说法缺一不可，这里返回 None 而不是跳过该条，理由同上。
        if not isinstance(situation, str) or not situation.strip():
            return None
        if not isinstance(style, str) or not style.strip():
            return None
        pairs.append(ExpressionPair(situation=situation.strip(), style=style.strip()))
    return pairs


def validate_pairs(pairs: Sequence[ExpressionPair]) -> tuple[List[ExpressionPair], int]:
    """落库前的前缀剥离与合法性检查。

    :param pairs: :func:`parse_learning` 校验过结构的二元组列表。
    :return: ``(可落库列表, 丢弃条数)``；前缀剥完为空、字段超长或超出单批上限的
        条目计入丢弃。
    副作用：无。
    """

    accepted: List[ExpressionPair] = []
    discarded = 0
    for pair in pairs:
        if len(accepted) >= MAX_PAIRS_PER_BATCH:
            discarded += 1
            continue
        situation = pair.situation.strip()
        style = strip_style_prefix(pair.style)
        if not situation or not style:
            discarded += 1
            continue
        if len(situation) > MAX_SITUATION_CHARS or len(style) > MAX_STYLE_CHARS:
            discarded += 1
            continue
        accepted.append(ExpressionPair(situation=situation, style=style))
    return accepted, discarded


async def learn_expressions(
    provider: LlmProvider,
    *,
    bot_name: str,
    dialogue: str,
    temperature: float,
    max_tokens: Optional[int],
) -> Optional[List[ExpressionPair]]:
    """请求模型从一段对话里找出 Bot 值得以后再用的说法。

    :param provider: 提供流式文本输出的模型客户端（复用 memory 任务槽）。
    :param bot_name: Bot 展示名，进系统提示词。
    :param dialogue: :func:`render_learning_dialogue` 产出的纯群友语料。
    :param temperature: 采样温度。
    :param max_tokens: 输出上限；``None`` 表示由 provider 决定。
    :return: 解析通过的二元组列表；对话过短返回空列表（合法「没东西可学」），
        模型调用失败或输出不合契约时返回 ``None``。
    副作用：发起一次流式模型请求并记录 ``llm_request`` 观测事件，不写数据库。
    """

    if len(dialogue) < MIN_DIALOGUE_CHARS:
        return []
    render_params = {'expression.learn': {'bot_name': bot_name}}
    result = await run_sub_agent(SubAgentCall(
        task='expression_learn',
        provider=provider,
        messages=[
            {'role': 'system', 'content': get_prompt('expression.learn').render(bot_name=bot_name)},
            {'role': 'user', 'content': dialogue},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={'type': 'json_object'},
        render_params=render_params,
        trace_extra=prompt_metadata('expression.learn', ('expression.learn',)),
    ))
    return parse_learning(result.text)


def persist_pairs(
    db: sqlite3.Connection,
    stream_id: int,
    pairs: Sequence[ExpressionPair],
    now: Optional[int] = None,
) -> int:
    """把校验通过的二元组写入 expressions 表。

    去重完全交给 ``UNIQUE(situation, style, stream_id)`` + ``INSERT OR IGNORE``，
    本函数不再叠任何一层判重。新行 ``checked`` 取默认 0（未复核），不进候选池：
    复核是放行闸门，仅人工确认的条目允许注入（见 ``agent/expression.py`` 的
    ``fetch_expression_pool``）。本函数的产出是待复核清单，不是立即生效的表达。

    :param db: 当前库连接。
    :param stream_id: 目标 stream ID；候选池严格按会话隔离，学到的归属原会话。
    :param pairs: :func:`validate_pairs` 通过的二元组列表。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: 实际插入的行数（去重命中的不计）。
    :raises sqlite3.Error: 写入失败时抛出。
    副作用：写入 ``expressions`` 并提交事务。
    """

    now = now if now is not None else current_time()
    inserted = 0
    for pair in pairs:
        cursor = db.execute(
            'INSERT OR IGNORE INTO expressions (situation, style, stream_id, source, created_at)'
            ' VALUES (?, ?, ?, ?, ?)',
            (pair.situation, pair.style, stream_id, LEARN_SOURCE, now),
        )
        inserted += cursor.rowcount
    db.commit()
    return inserted


def eliminate_stale(db: sqlite3.Connection, now: Optional[int] = None) -> int:
    """按确定性规则淘汰表达方式，返回删除条数。

    淘汰范围只有本模块学来的行（``source = LEARN_SOURCE``）。迁移存量不自动
    删除，由人工在 WebUI 中清理；不得将存量纳入淘汰的依据见模块 docstring。

    在这个范围内两道规则按序执行：

    1. 保留期：``checked = 0`` 且从未被选中（``last_used_at IS NULL``）且
       ``created_at`` 早于 ``now - RETENTION_MS`` 的行删除；
    2. 总量上限：学来的行仍超 :data:`MAX_LEARNED_ROWS` 时，在同样「未复核且
       从未被选中」的行里按 ``created_at`` 最旧继续删到达标。

    两道规则都只动 ``checked = 0`` 的行：人工确认（1）的永不淘汰；人工驳回（-1）
    是已判定的标记，删除后学习器可能再次提出同一内容。被选中过的行有使用
    证据，去留由使用频次决定，不在此删除。

    :param db: 当前库连接。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: 本次删除的总条数。
    :raises sqlite3.Error: 删除失败时抛出。
    副作用：删除 ``expressions`` 行并提交事务。
    """

    now = now if now is not None else current_time()
    cutoff = now - RETENTION_MS
    cursor = db.execute(
        'DELETE FROM expressions'
        ' WHERE source = ? AND checked = 0 AND last_used_at IS NULL'
        ' AND created_at < ?',
        (LEARN_SOURCE, cutoff),
    )
    deleted = cursor.rowcount
    # 上限只数本模块学来的行：全表计数会把 3000 多条存量算进来，excess 恒为正，
    # 每轮都在删刚学到的东西。
    learned_rows = int(db.execute(
        'SELECT COUNT(*) FROM expressions WHERE source = ?',
        (LEARN_SOURCE,),
    ).fetchone()[0])
    excess = learned_rows - MAX_LEARNED_ROWS
    if excess > 0:
        cursor = db.execute(
            'DELETE FROM expressions WHERE id IN ('
            '   SELECT id FROM expressions'
            '   WHERE source = ? AND checked = 0 AND last_used_at IS NULL'
            '   ORDER BY created_at ASC, id ASC LIMIT ?)',
            (LEARN_SOURCE, excess),
        )
        deleted += cursor.rowcount
    # 无条件提交：DELETE 即使一行未删也已开启隐式写事务，不提交会让连接挂着
    # RESERVED 锁，同库其他连接（如事件账本）随后的写入全部撞「database is locked」。
    db.commit()
    return deleted


async def run_learning(
    store: MemoryStore,
    provider: LlmProvider,
    db: sqlite3.Connection,
    *,
    stream_id: int,
    participants: Sequence[Participant],
    bot_name: str,
    trigger_messages: int = TRIGGER_MESSAGES,
    batch_messages: int = BATCH_MESSAGES,
    temperature: float,
    max_tokens: Optional[int],
    now: Optional[int] = None,
) -> Optional[LearnReport]:
    """检查触发条件并完成一次学习与淘汰。

    调用方（``ChatService``）在回合收尾处调用；属后台任务，不放在回复关键
    路径上，失败不阻塞回合。同一 stream 的并发去重由调用方负责，
    形态与 ``_maybe_extract_facts`` 的内存集合一致。

    :param store: 记忆存储实例。
    :param provider: 学习任务的模型客户端（复用 memory 任务槽）。
    :param db: 当前库连接。
    :param stream_id: 目标 stream ID。
    :param participants: 本批消息的在场者，只用于把群友发言渲染成带名字的行，
        学习不做人物归属判定。
    :param bot_name: Bot 展示名。
    :param trigger_messages: 游标之后累积多少条消息才触发一次学习。
    :param batch_messages: 单次交给模型的消息条数。
    :param temperature: 采样温度。
    :param max_tokens: 输出上限。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: 学习对账；未达触发条件、模型失败或输出不合契约时返回 ``None``
        （后两种情况不推进游标，下次重跑同一批）。
    :raises sqlite3.Error: 落库或淘汰失败时抛出。
    副作用：可能发起一次模型请求、写入 expressions、推进游标、删除过期条目，
        并发出观测事件。
    """

    now = now if now is not None else current_time()
    cursor = read_cursor(store, stream_id)
    if store.message_count_after(stream_id, cursor) < trigger_messages:
        return None
    batch = store.messages_after(stream_id, cursor, batch_messages)
    if not batch:
        return None
    # 表达学习模型只看群友语料：助手的真实发言与动作历史不进入请求，
    # 在代码层移除，不依赖提示词要求模型忽略。
    dialogue = render_learning_dialogue(batch, participants, bot_name)
    if dialogue:
        pairs = await learn_expressions(
            provider,
            bot_name=bot_name,
            dialogue=dialogue,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if pairs is None:
            # 模型故障或输出不合契约：不推进游标，下次重跑同一批，重复学习
            # 优于永久跳过该段对话。
            trace.emit('expression_learn_failed', streamId=stream_id, cursor=cursor)
            return None
        accepted, discarded = validate_pairs(pairs)
        learned = persist_pairs(db, stream_id, accepted, now)
        candidates = len(pairs)
    else:
        learned, discarded, candidates = 0, 0, 0
    advance_cursor(store, stream_id, batch[-1].message_id)
    # 淘汰挂在学习之后同一次调用里，不另起后台任务。它清的是全表（各会话的过期
    # 条目一起清），由哪条会话触发并不重要。
    eliminated = eliminate_stale(db, now)
    remaining = int(db.execute('SELECT COUNT(*) FROM expressions').fetchone()[0])
    trace.emit(
        'expression_learned',
        streamId=stream_id,
        messageCount=len(batch),
        candidates=candidates,
        learned=learned,
        discarded=discarded,
        eliminated=eliminated,
        remaining=remaining,
    )
    return LearnReport(
        message_count=len(batch),
        candidates=candidates,
        learned=learned,
        discarded=discarded,
        eliminated=eliminated,
        remaining=remaining,
    )
