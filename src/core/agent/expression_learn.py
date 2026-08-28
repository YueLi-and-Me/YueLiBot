"""从已经发生的对话里学习「什么情境下怎么说话」，并负责词表的淘汰。

表达方式词表此前只出不进：全部条目来自一次性迁移（``历史迁移:M-2``），运行时
不存在任何 INSERT 路径。本模块补上「学」这一段，形态照搬同代码库已跑通的
:func:`src.core.agent.fact_extract.run_extraction`——回合之后的后台任务、独立
游标、按阈值触发、失败只丢该批。

语料口径（为什么这样取）：

- 取游标后的一批消息，**双方的话都在内**，渲染成带说话人的逐行对话。
  situation 描述的是「那句话是在什么情境下说的」，情境只能从双方的来往里
  读出来（「被夸的时候」这种情境，光看她自己的行是看不出来的）。
- 但 style 必须锚定**她亲口说过**的说法（提示词里硬性要求）。expressions 表
  注入的是「她的表达习惯参考」，学别人的原话当她的说法，人格一致性没有保障；
  她亲口说过的说法是被真实语境验证过的。
- 本批她一句话都没说时直接判「没东西可学」，游标照常推进——这不是失败，
  重跑同一批也不会多出她的发言。

核心职责与对外接口：

- :func:`read_cursor` / :func:`advance_cursor`：学习自己的进度游标，存在
  ``meta`` 键值表里。它是 ``messages_after`` 的第三个独立消费者（摘要用
  ``episode_id`` 队列、事实抽取用自己的游标），共用判据会让消费者互相吃掉
  输入且不报错。
- :func:`strip_style_prefix` / :func:`validate_pairs`：落库前的前缀剥离与
  合法性检查。旧学习提示词的示例以「使用」开头，模型把前缀一并学回来，
  造成存量 42% 的 style 是「使用反问句加强语气」这类指令句式；新学条目
  不得重蹈（口径与 ``scripts/fix_expression_style_prefix.py`` 同源）。
- :func:`parse_learning`：严格校验模型输出，结构非法即整批丢弃。
- :func:`persist_pairs`：去重完全交给 ``UNIQUE(situation, style, stream_id)``
  + ``INSERT OR IGNORE``；字面不同但语义重复的条目会堆积，由淘汰兜住，
  不引入相似度模型。
- :func:`eliminate_stale`：确定性淘汰，与学习挂在同一次调用里，不另起后台任务。
- :func:`run_learning`：上面几步的编排，调用方只提供 store、provider 与批次参数。

淘汰规则（刻意确定、可解释，不用模型判断删哪条）：

- **只淘汰本模块自己学来的行**（``source = LEARN_SOURCE``）。迁移带来的存量
  一条都不自动删，要清理由人在 WebUI 上手删。
  这条是硬约束，不要因为「存量绝大多数没被本机选中过」就把它们纳进来：
  真机 3360 条存量分属两个会话，其中一个会话的行**全部**从未被本机选中过
  （2026-08-26 实测 stream 5 有 1752 条、``last_used_at`` 非空 0 条）。
  按「未被选中即淘汰」清一遍，那个会话的候选池会直接归零，
  低于 ``agent/expression.py`` 的 ``MIN_POOL_CANDIDATES``，
  表达选择在该会话彻底停摆——而学习器要补满需要很久。
- 「从未在本机被选中」的判据是 ``last_used_at IS NULL``，不是 ``use_count = 0``：
  本模块学到的行两列同处回写（选中才同时加一、打时间戳），两者等价；而存量的
  ``use_count`` 是旧部署带过来的历史值，不能当作本机使用证据。范围既然已经
  限定在本模块学来的行上，两个判据在实践中等价，取 ``last_used_at`` 是因为
  它的语义不依赖数据来源。
- ``checked = 1``（人工确认）的永不淘汰：人明确说好的东西不能被后台任务悄悄
  删掉。``checked = -1``（人工驳回）的同样不删：驳回是「这条判过了」的记号，
  删掉它学习器下次可能又学回来；它靠候选池 SQL 排除来停止生效，不靠删除。
- 总量上限只约束本模块学来的行，与存量条数无关——否则存量本身就超过上限，
  上限会退化成「每轮都在删新学的」。

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
from src.core.memory.store import MemoryStore, is_assistant_action_message
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt, prompt_metadata

logger = get_logger(__name__)

# 游标存在 meta 键值表里，与事实抽取的 fact_extract_cursor、摘要的 episode_id
# 队列完全解耦；不新建表、不加列、不占迁移号。
CURSOR_KEY = 'expression_learn_cursor'
# 本机学习的来源标识，与迁移存量（历史迁移:M-2）在 WebUI 行尾一眼分清。
LEARN_SOURCE = '本机学习'

# 触发阈值与批大小，单位都是「消息条数」。表达信号比人物事实稀疏：批次 16 条
# 约覆盖 6~10 个来回，模型才看得见「那句话是在什么情境下说的」；阈值 24 让
# 活跃会话每天最多学几次。与事实抽取的 32/12 刻意不同值——两个消费者各走各的
# 游标，取值没有联动必要。
TRIGGER_MESSAGES = 24
BATCH_MESSAGES = 16

# 校验上限，单位字符。真机存量 style 最长 31、situation 最长 39（2026-08-26
# 实测），上限取约 1.5 倍余量；注入模板会把 style 原文拼进系统提示词，超长
# 条目既稀释提示词预算，也多半是模型跑偏的产物。
MAX_SITUATION_CHARS = 60
MAX_STYLE_CHARS = 60
# 单批最多落库条数。提示词要求最多 5 条，硬上限放宽到 8 条挡失控输出。
MAX_PAIRS_PER_BATCH = 8

# 淘汰保留期，单位毫秒（30 天）。候选池每轮只抽 10 条（agent/expression.py），
# 一条**本模块学来的**说法一个月都没被选中过，说明它在这个会话里没有适配场景。
# 存量不适用：它们的入库时间是迁移那天，与「学了多久没被用」无关。
RETENTION_MS = 30 * 24 * 60 * 60 * 1000
# 本模块学来的行的总量上限（条）。注意口径是「学来的行」而不是全表：存量本身
# 就有 3360 条，按全表算这个上限会退化成每轮都在删刚学到的东西。取池按会话
# 全取后内存抽样，学习产出无限长只会让扫描与长尾噪声一起涨；2000 已远超
# 抽样能覆盖的量级。
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
    """请求模型从一段对话里找出她值得以后再用的说法。

    :param provider: 提供流式文本输出的模型客户端（复用 memory 任务槽）。
    :param bot_name: Bot 展示名，进系统提示词。
    :param dialogue: :func:`render_dialogue` 的产物。
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
    本函数不再叠任何一层判重。新行 ``checked`` 取默认 0（未复核）——未复核照常
    进候选池，复核的职责是剔除与保护，不是放行。

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

    **淘汰范围只有本模块自己学来的行**（``source = LEARN_SOURCE``）。迁移带来的
    存量一条都不自动删，清理由人在 WebUI 上手动进行。理由见模块 docstring：
    存量里有整个会话的行从未被本机选中过，纳入淘汰会让该会话候选池归零、
    表达选择停摆。

    在这个范围内两道规则按序执行：

    1. 保留期：``checked = 0`` 且从未被选中（``last_used_at IS NULL``）且
       ``created_at`` 早于 ``now - RETENTION_MS`` 的行删除；
    2. 总量上限：学来的行仍超 :data:`MAX_LEARNED_ROWS` 时，在同样「未复核且
       从未被选中」的行里按 ``created_at`` 最旧继续删到达标。

    两道规则都只动 ``checked = 0`` 的行：人工确认（1）的永不淘汰，人工驳回（-1）
    的是「判过了」的记号，删了学习器可能又学回来。被选中过的行不删——它被真实
    用过，去留交给使用频次的自然筛选，后台任务不删有使用证据的行。

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

    调用方（``ChatService``）在回合收尾处调用，**不要放进回复的关键路径**：
    它是后台任务，失败不阻塞任何回合。同一 stream 的并发去重由调用方负责，
    形态与 ``_maybe_extract_facts`` 的内存集合一致。

    :param store: 记忆存储实例。
    :param provider: 学习任务的模型客户端（复用 memory 任务槽）。
    :param db: 当前库连接。
    :param stream_id: 目标 stream ID。
    :param participants: 本批消息的在场者，只用于把对话渲染成带名字的行，
        学习不做归属判定。
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
    # 本批她一句话都没说时没什么可学（style 必须锚定她亲口说过的说法）。助手
    # 动作伪消息只供后续回合回看，不是她说出口的语料；游标仍照常推进。
    she_spoke = any(
        message.role == 'assistant'
        and (message.content or '').strip()
        and not is_assistant_action_message(message.content)
        for message in batch
    )
    if she_spoke:
        pairs = await learn_expressions(
            provider,
            bot_name=bot_name,
            dialogue=render_dialogue(batch, participants, bot_name),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if pairs is None:
            # 模型故障或输出不合契约：不推进游标，下次重跑同一批。宁可重复学一次，
            # 也不要因为一次故障永久跳过这段对话。
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
