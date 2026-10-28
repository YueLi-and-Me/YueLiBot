"""从已经发生的对话里学习「什么情境下怎么说话」，并负责词表的淘汰。

运行时此前不存在表达词表的写入路径（全部条目来自一次性迁移）。本模块补充学习侧，
形态与 :func:`src.core.agent.fact_extract.run_extraction` 一致：回合后的后台任务、
独立游标、按阈值触发、失败仅丢弃该批。

语料口径：

- 游标与处理对账覆盖整批消息，送入表达学习模型的语料按「单元」组织：同一说话人
  连续发出的消息（中间没有别人开口）合成一个单元，单元内各条用「／」连接、一个
  单元一行。她的真实发言以「{bot_name}（她自己）」标注后保留在语料里，助手动作
  伪消息整条跳过。
- 旧做法把她的全部发言在代码层移除，理由是阻断自举闭环，但代价是群友接她话、
  学她说话时模型看不到被接的原话：群友原样复述她两句前说过的说法，模型会把这次
  复读当成一条独立的新说法学进库，等于绕一圈继续强化她的口癖。
- 新做法用两道防线代替整段删除：

  1. 来源校验：`source` 必须是本批群友单元。标「她自己」的单元、批次之前的
     前文单元（只作上下文）与越界编号一律以 `self` 拦截。
  2. 复读检测：`kind = 复读学舌` 直接以 `echo_kind` 拦截；`kind = 说法`
     的原话若在更早单元（任何人，含她自己）里出现过记 `echo`；`kind = 固定说法`
     的原话若只在她更早的单元里出现过记 `echo_self`；`kind = 说法` 的
     `style` 与原话在去空白、去首尾标点后相同记 `copy`。

  - 现象：2026-08-28 表达库积累 9 条几乎同义的「……也没用」，全部是 Bot 本人
    08-17 至 08-21 原话的逐字转录；同期该句式在 Bot 发言中的占比从 0 升至 7.3%。
  - 原因：学习自身发言构成自举闭环：Bot 的发言被记录后注入提示词，再次影响
    发言，词表缺少外部输入，既有倾向被持续放大。
  - 后果：口癖被持续强化，且以「表达习惯」形式从隐性模仿变为显式指令。
  - 旧做法的缺口：删掉她的发言并不能解除闭环。群友复读她的说法时，被复读的原文
    不在输入里，模型看到的是「群友说了一句很有辨识度的话」，于是把复读学成新条目，
    下一次注入又把同一口癖还给她。保留她的发言作为上下文、在代码层按来源与复读
    拦截，才能同时读到被接的原话并拒绝学它。
  注入一致性由复核闸门保障，每条须经人工确认才进入候选池（见
  `agent/expression.py` 的 `fetch_expression_pool`）。
- 本批除 Bot 之外无人发言时直接判无可学内容，游标照常推进：重跑同一批也
  不会产生新的群友发言。

核心职责与对外接口：

- :func:`read_cursor` / :func:`advance_cursor`：学习自己的进度游标，存在
  `meta` 键值表里。它是 `messages_after` 的第三个独立消费者（摘要用
  `episode_id` 队列、事实抽取用自己的游标），共用判据会让消费者相互消费对方的输入且不报错。
- :func:`collect_learning_batch`：按「数够 16 条群友消息」切批，并回看批次之前
  6 条消息作为前文；前文与批次里的她的发言都保留，只作上下文。
- :func:`render_learning_corpus`：把前文与批次渲染成带编号的单元语料，前文单元
  行首标「（前文）」。
- :func:`strip_style_prefix` / :func:`validate_pairs`：落库前的前缀剥离与六字段
  校验。旧迁移存量 42% 的 style 为「使用反问句加强语气」这类指令句式，源于旧
  提示词示例；新学条目剥离同类前缀（口径与
  `scripts/maintain/fix_expression_style_prefix.py` 同源）。校验按原因码计数
  拦截，中文名见 :data:`DROP_REASON_LABELS`。
- :func:`parse_learning`：严格校验模型输出，结构非法即整批丢弃。
- :func:`persist_pairs`：写入 `reading`、`quote`，新行 `checked = 0`；命中
  `UNIQUE(situation, style, stream_id)` 时 `learn_count + 1` 并更新
  `reading`/`quote`，不改 `checked`。字面不同但语义重复的条目会堆积，
  由淘汰兜住，不引入相似度模型。
- :func:`eliminate_stale`：确定性淘汰，与学习挂在同一次调用里，不另起后台任务。
- :func:`run_learning`：上面几步的编排，调用方只提供 store、provider 与批次参数。

淘汰规则为确定性规则，可解释，不用模型判断删哪条：

- 只淘汰本模块学来的行（`source = LEARN_SOURCE`）；迁移存量不自动删除，
  清理由人工在 WebUI 中进行。不得将存量纳入「未被选中即淘汰」：存量 3360 条
  分属两个会话，其中会话 5 的行全部未被本机选中过（2026-08-26 实测 1752 条、
  `last_used_at` 非空 0 条），纳入后该会话候选池归零，低于
  `agent/expression.py` 的 `MIN_POOL_CANDIDATES`，表达选择停摆。
- 「从未在本机被选中」的判据是 `last_used_at IS NULL`，不是 `use_count = 0`：
  本模块学到的行两列同时回写（选中才同时加一、打时间戳），两判据等价；存量
  的 `use_count` 是旧部署的历史值，不构成本机使用证据。取 `last_used_at`
  是其语义不依赖数据来源。
- `checked = 1`（人工确认）的行永不淘汰：人工确认的条目不得被后台任务删除。
  `checked = -1`（人工驳回）同样不删：驳回是已判定的标记，删除后学习器可能
  再次提出同一内容；其失效由候选池 SQL 排除实现，不依赖删除。
- 复核为放行闸门（只有 `checked = 1` 进候选池）之后，未复核的行不可能被
  选中，`last_used_at` 恒为 NULL，「从未被选中」不构成价值判断，而是必然
  状态。:data:`RETENTION_MS` 的实际语义是复核期限：超期未复核的建议被清除，
  由学习器在遇到同样说法时重新提出。
- 总量上限只约束本模块学来的行，与存量条数无关；否则按全表计算时上限退化为
  每轮删除新学条目。

依赖：`src.core.memory.store`（读消息与游标）、`src.core.agent.sub_agent`
（统一模型执行器）、`src.core.prompts.registry`（`expression.learn` 模板）、
`src.core.observe`（观测事件）。被 `src.core.services.chat` 在回合收尾处调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import json
import re
import sqlite3

from .fact_extract import Participant
from .history import strip_say_tags, strip_side_effect_tags
from .sub_agent import SubAgentCall, run_sub_agent

from src.core.llm_models.protocol import LlmProvider
from src.core.logging.logger import get_logger
from src.core.memory.store import MemoryStore, StoredMessage, is_assistant_action_message
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt, prompt_metadata
from src.core.runtime.clock import now as current_time

logger = get_logger(__name__)

# 游标存在 meta 键值表里，与事实抽取的 fact_extract_cursor、摘要的 episode_id
# 队列完全解耦；不新建表、不加列、不占迁移号。
CURSOR_KEY = 'expression_learn_cursor'
# 本机学习的来源标识，与迁移存量（历史迁移:M-2）便于按 source 区分。
LEARN_SOURCE = '本机学习'

# 触发阈值与批大小，单位都是「消息条数」。触发阈值按全部消息计（含她的发言与
# 动作伪消息），批次口径则只数群友消息：表达信号比群友发言稀疏，16 条群友消息
# 约覆盖 6~10 个来回，足以呈现说法所在的情境；阈值 24 限制活跃会话的学习频次。
# 与事实抽取的 32/12 取值不同：两个消费者各用各的游标，取值无联动。
TRIGGER_MESSAGES = 24
BATCH_PEER_MESSAGES = 16
# 批次之前额外带上的前文条数，单位条。前文只作上下文，不能作为候选来源；
# 群友接她话、学她说话时，被接的原话往往落在前文里。
LEAD_CONTEXT_MESSAGES = 6
# 分块读取消息的块大小，单位条。块大小只影响读取次数，不改变批次口径。
_COLLECT_CHUNK = 64

# 校验上限，单位字符。situation 60、style 80、reading 200、quote 80。真机存量
# style 最长 31、situation 最长 39（2026-08-26 实测），上限取约 1.5 倍余量；
# 注入模板会把 style 原文拼进系统提示词，超长条目占用提示词预算且多为异常输出。
# reading 与 quote 是新增字段，长度上限只用于拦截异常长输出。
MAX_SITUATION_CHARS = 60
MAX_STYLE_CHARS = 80
MAX_READING_CHARS = 200
MAX_QUOTE_CHARS = 80
# 单批最多落库条数。提示词要求一批通常 0～1 条、最多 3 条；硬上限与提示词
# 一致，不做额外放宽——表达信号稀疏，超量输出多为把普通句子也当成说法。
MAX_PAIRS_PER_BATCH = 3

# 三种表达类型的取值，原样进提示词与校验，不落库为独立列。含义：说法=依赖具体
# 人和事才成立的结构；固定说法=不指向任何人和物、原样说也成立的口头说法；
# 复读学舌=复读别人或学别人说话（含学她）。
KIND_STATEMENT = '说法'
KIND_FIXED = '固定说法'
KIND_ECHO = '复读学舌'
EXPRESSION_KINDS = (KIND_STATEMENT, KIND_FIXED, KIND_ECHO)

# 提示词示例里出现的 style 原文。表达学习原则上不能抄示例，否则会反复产出同一句；
# 校验时按 example 原因码拦截。这里与 expression.learn.md 的示例保持一致，
# 修改提示词示例必须同步本常量。
PROMPT_EXAMPLE_STYLES = (
    '你这哪是〈某样东西〉，你这是给〈某个地方〉守门',
    '行嘞',
)

# 拦截原因码到中文名的映射。原因码是稳定英文标识，写进日志与事件；中文名只用于
# 日志展示与测试可读，日志模块不反向依赖本模块。
DROP_REASON_LABELS: Dict[str, str] = {
    'self': '来源不合规',
    'quote': '原话不在来源',
    'echo_kind': '复读学舌',
    'echo': '原样复读',
    'echo_self': '学她的说法',
    'copy': '整句照抄',
    'empty': '字段为空',
    'fixed_mismatch': '固定说法不符',
    'invented': '说法不出自原话',
    'name': '含人名',
    'length': '超出长度',
    'example': '照搬示例',
    'limit': '超单批上限',
}

# copy 判定里要剥掉的首尾标点：中英文常见标点与成对引号/括号。模型常把同一句
# 话既填进 quote 又填进 style，是否带句号/引号不应影响判定，但内容本身必须
# 一字不差（只在归一化之后的字符串首尾剥离，不碰中间）。
_TRIM_PUNCTUATION = frozenset(
    '，。！？、；：…—～·「」『』（）〔〕【】《》〈〉“”‘’'
    '"(),.!?;:[]{}<>-'
)

# 旧学习提示词带出来的前导词。剥离按「最长在前」逐轮进行：「使用」先于「用」匹配，
# 剥完一轮再检查一轮，挡住「使用用……」这类叠写。
_LEADING_PREFIXES = ('使用', '采用', '用')

# 引用前缀，形如 [回复 某人：被引用的原文]。它属于协议包装而不是说话人原话，
# 建立 quote_body 时从头剥掉；quote 校验与展示都以剥掉之后的正文为准。
_QUOTE_PREFIX = re.compile(r'^\[回复 [^\]]*\]\s*')

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
class LearningUnit:
    """语料中的一个发言单元：同一说话人连续发出的若干条消息。

    :ivar label: 单元编号，形如 `L1`、`L2`，前文与批次连续编号。
    :ivar speaker: 渲染用的说话人前缀，群友为 `[平台号] 显示名`，
        她本人为 `{bot_name}（她自己）`。
    :ivar rendered: 单元正文；保留引用前缀，多条消息用「／」连接。
    :ivar quote_body: 可摘录正文；去掉引用前缀，多条消息同样用「／」连接。
    :ivar is_lead: 是否属于批次之前的前文；前文只作上下文，不能当来源。
    :ivar is_self: 是否为她本人的发言单元；她的单元只作上下文，不能当来源。
    :ivar message_ids: 本单元覆盖的消息主键，按出现顺序。
    """

    label: str
    speaker: str
    rendered: str
    quote_body: str
    is_lead: bool
    is_self: bool
    message_ids: Tuple[int, ...]


@dataclass(frozen=True)
class LearningBatch:
    """一次学习调用要处理的批次及其前文。

    :ivar lead: 批次第一条消息之前的若干条消息，按 ID 正序；只作上下文。
    :ivar batch: 本批消息，按 ID 正序；批次止于第 16 条群友消息。
    """

    lead: Tuple[StoredMessage, ...]
    batch: Tuple[StoredMessage, ...]


@dataclass(frozen=True)
class ExpressionPair:
    """模型输出的一条待落库表达，六个字段与提示词契约一致。

    :ivar source: 来源单元编号，从 1 开始；必须是本批群友单元。
    :ivar quote: 从来源单元原样摘出的那段话，用于人工核对与去重展示。
    :ivar reading: 指代关系说明：在接谁、指谁指什么、正话还是反话等。
    :ivar kind: :data:`EXPRESSION_KINDS` 之一。
    :ivar situation: 适用情境描述，注入提示词时只展示这一字段供选择模型判断。
    :ivar style: 该情境下的说法句式，选中之后才作为载荷拼进注入文本。
    """

    source: int
    quote: str
    reading: str
    kind: str
    situation: str
    style: str


@dataclass(frozen=True)
class LearnReport:
    """一次学习调用的完整对账。

    :ivar message_count: 本批交给模型的消息条数（前文 + 批次）。
    :ivar units: 渲染出的语料单元数。
    :ivar candidates: 模型按契约返回的原始候选条数。
    :ivar learned: 实际新增的条数。
    :ivar increased: 命中既有 (situation, style, stream_id) 而 learn_count 加一的条数。
    :ivar dropped: 按原因码分组的拦截计数，键见 :data:`DROP_REASON_LABELS`。
    :ivar eliminated: 本次淘汰删除的条数。
    :ivar remaining: 淘汰后全表剩余条数。
    """

    message_count: int
    units: int
    candidates: int
    learned: int
    increased: int
    dropped: Dict[str, int]
    eliminated: int
    remaining: int


def read_cursor(store: MemoryStore, stream_id: int) -> int:
    """读取某个 stream 的学习进度游标。

    :param store: 记忆存储实例。
    :param stream_id: 目标 stream ID。
    :return: 已学习到的最后一条消息 ID；从未学习过时返回 `0`。
    副作用：只读 `meta` 表。
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
    副作用：写入 `meta` 表并提交事务。
    """

    raw = store.read_json(CURSOR_KEY, {})
    cursor: Dict[str, int] = dict(raw) if isinstance(raw, dict) else {}
    cursor[str(stream_id)] = int(message_id)
    store.write_json(CURSOR_KEY, cursor)


def _is_peer_message(message: StoredMessage) -> bool:
    """判断一条消息是否计入「群友消息」批次名额。

    口径：角色为 `user` 且正文非空。她的真实发言与动作伪消息都不占名额，
    但会照样带进批次，供模型读懂上下文。

    :param message: 待判断的消息。
    :return: 计入群友消息名额时为 `True`。
    副作用：无。
    """

    return message.role == 'user' and bool((message.content or '').strip())


def collect_learning_batch(
    store: MemoryStore,
    stream_id: int,
    cursor: int,
) -> Optional[LearningBatch]:
    """从游标之后切出一批学习语料，并读取批次之前的前文。

    批次口径：按 ID 顺序累计群友消息，数到第 16 条为止，其后的消息不进本批；
    她的发言与动作伪消息夹在中间照样带上，但不占名额。消息流在本批不足 16 条
    群友消息时，取到末尾为止。游标之后一条群友消息都没有时返回 `None`：
    没有可学的来源，调用方不应推进游标。

    :param store: 记忆存储实例。
    :param stream_id: 目标 stream ID。
    :param cursor: 已处理到的最后一条消息 ID；`0` 表示从头开始。
    :return: 批次与前文；没有群友消息时为 `None`。
    :raises sqlite3.Error: 读消息失败时抛出。
    副作用：只读 messages 表。分块读取（每块 :data:`_COLLECT_CHUNK` 条），
        不一次把整个会话读进内存。
    """

    collected: List[StoredMessage] = []
    peer_count = 0
    next_id = cursor
    while peer_count < BATCH_PEER_MESSAGES:
        chunk = store.messages_after(stream_id, next_id, _COLLECT_CHUNK)
        if not chunk:
            break
        for message in chunk:
            collected.append(message)
            if _is_peer_message(message):
                peer_count += 1
                if peer_count >= BATCH_PEER_MESSAGES:
                    break
        next_id = chunk[-1].message_id
    if not collected or peer_count == 0:
        return None
    lead = store.messages_before(
        stream_id,
        collected[0].message_id,
        LEAD_CONTEXT_MESSAGES,
    )
    return LearningBatch(lead=tuple(lead), batch=tuple(collected))


def _flatten_body(text: str) -> str:
    """把一条消息的正文压成一行，口径与 :func:`fact_extract.render_dialogue` 相同。

    多气泡回复经 :func:`strip_say_tags` 后是换行分隔的；不压平会产生没有说话人
    前缀的续行，模型无从判断那句是谁说的。

    :param text: 已剥协议标签的正文。
    :return: 单行正文；空文本返回空字符串。
    副作用：无。
    """

    return ' '.join(part for part in text.splitlines() if part.strip()).strip()


def _render_unit_parts(
    message: StoredMessage,
    participants: Sequence[Participant],
    bot_name: str,
) -> Optional[Tuple[str, str, str]]:
    """把一条消息渲染成单元片段：`(说话人, 正文, 可摘录正文)`。

    助手动作伪消息与空正文返回 `None`，由调用方整条跳过、且不打断单元合并。

    :param message: 待渲染的消息。
    :param participants: 本批在场者，用于把 `sender_person_id` 还原成带编号的说话人。
    :param bot_name: Bot 展示名，用于标注她自己的发言。
    :return: 说话人前缀、保留引用前缀的正文、去掉引用前缀的可摘录正文；
        跳过该消息时返回 `None`。
    副作用：不修改输入消息。
    """

    if message.role == 'assistant':
        # 动作伪消息只供聊天历史回看，不能被模型当成亲口说过的话。
        if is_assistant_action_message(message.content):
            return None
        text = strip_say_tags(strip_side_effect_tags(message.content or ''))
        body = _flatten_body(text)
        if not body:
            return None
        return f'{bot_name}（她自己）', body, body
    body = _flatten_body(message.content or '')
    if not body:
        return None
    person = next(
        (p for p in participants if p.person_id == message.sender_person_id),
        None,
    )
    # 群聊里昵称会重复也会改，编号是唯一稳定的锚；找不到在场者时退回「某人」，
    # 与事实抽取的渲染口径一致。
    speaker = f'[{person.external_id}] {person.display_name}' if person else '某人'
    return speaker, body, _QUOTE_PREFIX.sub('', body, count=1)


def _build_units(
    messages: Sequence[StoredMessage],
    participants: Sequence[Participant],
    bot_name: str,
    *,
    is_lead: bool,
    start_index: int,
) -> List[LearningUnit]:
    """把一段消息渲染成按说话人合并后的单元列表。

    同一说话人连续的消息合并进同一个单元；动作伪消息与空正文被跳过，既不参与
    合并也不断开合并。单元编号从 `start_index` 之后继续。

    :param messages: 该段的全部消息，按 ID 正序。
    :param participants: 本批在场者。
    :param bot_name: Bot 展示名。
    :param is_lead: 该段是否为批次之前的前文。
    :param start_index: 已生成的单元数，用于连续编号。
    :return: 新生成的单元列表，编号连续。
    副作用：无。
    """

    units: List[LearningUnit] = []
    for message in messages:
        parts = _render_unit_parts(message, participants, bot_name)
        if parts is None:
            continue
        speaker, rendered, quote_body = parts
        is_self = message.role == 'assistant'
        if units and units[-1].speaker == speaker:
            previous = units[-1]
            units[-1] = LearningUnit(
                label=previous.label,
                speaker=speaker,
                rendered=f'{previous.rendered}／{rendered}',
                quote_body=f'{previous.quote_body}／{quote_body}',
                is_lead=is_lead,
                is_self=is_self,
                message_ids=(*previous.message_ids, message.message_id),
            )
            continue
        label = f'L{start_index + len(units) + 1}'
        units.append(LearningUnit(
            label=label,
            speaker=speaker,
            rendered=rendered,
            quote_body=quote_body,
            is_lead=is_lead,
            is_self=is_self,
            message_ids=(message.message_id,),
        ))
    return units


def render_learning_corpus(
    lead: Sequence[StoredMessage],
    batch: Sequence[StoredMessage],
    participants: Sequence[Participant],
    bot_name: str,
) -> Tuple[str, List[LearningUnit]]:
    """把前文与批次渲染成表达学习语料。

    前文与批次分开构建单元、编号连续；前文单元行首另标「（前文）」，只作上下文。
    每单元一行，行格式：前文 `L1（前文）[编号] 名字：正文`，批次
    `L3[编号] 名字：正文`；她自己的单元以 `{bot_name}（她自己）` 作说话人。

    :param lead: 批次之前的前文消息，按 ID 正序。
    :param batch: 本批消息，按 ID 正序。
    :param participants: 本批在场者，只用于还原群友的展示名与平台编号。
    :param bot_name: Bot 展示名。
    :return: `(逐行语料, 单元列表)`；语料为空表示没有可交给模型的内容。
    副作用：不修改输入消息，不改 :func:`fact_extract.render_dialogue`。
    """

    lead_units = _build_units(
        lead,
        participants,
        bot_name,
        is_lead=True,
        start_index=0,
    )
    batch_units = _build_units(
        batch,
        participants,
        bot_name,
        is_lead=False,
        start_index=len(lead_units),
    )
    lines = [
        f'{unit.label}（前文）{unit.speaker}：{unit.rendered}' if unit.is_lead
        else f'{unit.label}{unit.speaker}：{unit.rendered}'
        for unit in (*lead_units, *batch_units)
    ]
    return '\n'.join(lines), [*lead_units, *batch_units]


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
    """从模型输出中解析表达候选列表。

    契约是一个对象而非裸数组，与事实抽取同一条纪律：裸数组无法区分「模型没按
    契约输出」与「这批没什么可学的」，前者必须整批判废。结构校验只做到
    「字段类型与枚举正确」，内容为空或超长留给 :func:`validate_pairs` 按原因码
    记录，避免一处失败把整批候选连带丢掉。

    :param raw: 可能带 Markdown 代码围栏或额外说明的模型输出。
    :return: 解析出的候选列表（空列表是合法结果，表示这批没什么可学的）；
        JSON 非法、顶层不是对象、缺 `expressions` 键、条目非对象、`source`
        不是正整数、`kind` 不在 :data:`EXPRESSION_KINDS`、四个文本字段不是
        字符串时返回 `None` 表示整批丢弃。
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
        source = item.get('source')
        # bool 是 int 的子类，必须显式排除，否则 true 会被当成来源编号 1。
        if isinstance(source, bool) or not isinstance(source, int) or source <= 0:
            return None
        kind = item.get('kind')
        if kind not in EXPRESSION_KINDS:
            return None
        quote = item.get('quote')
        reading = item.get('reading')
        situation = item.get('situation')
        style = item.get('style')
        if not all(isinstance(value, str) for value in (quote, reading, situation, style)):
            return None
        pairs.append(ExpressionPair(
            source=source,
            quote=quote,
            reading=reading,
            kind=kind,
            situation=situation,
            style=style,
        ))
    return pairs


def _normalized(text: str) -> str:
    """去掉全部空白字符后的文本，用于复读检测的「出现过」判定。

    模型可能把同一句话里的空格、换行略作改动；判定复读时这些差异不应放过。
    标点一律保留，改标点不算复读。

    :param text: 原始文本。
    :return: 删除了全部空白字符的文本。
    副作用：无。
    """

    return ''.join(text.split())


def _trimmed_punctuation(text: str) -> str:
    """去掉文本首尾的常见标点，用于「整句照抄」判定。

    :param text: 已去空白的文本。
    :return: 两端标点被剥净的文本；中间字符一律不改。
    副作用：无。
    """

    start = 0
    end = len(text)
    while start < end and text[start] in _TRIM_PUNCTUATION:
        start += 1
    while end > start and text[end - 1] in _TRIM_PUNCTUATION:
        end -= 1
    return text[start:end]


def _appears_in(units: Sequence[LearningUnit], quote: str, *, self_only: bool) -> bool:
    """判断一段原话是否出现在给定单元集合里（去空白后按子串判定）。

    :param units: 待检查的单元，调用方已截取「更早的单元」。
    :param quote: 已确认非空的原话。
    :param self_only: 为 `True` 时只检查她的单元，用于固定说法的学舌检测。
    :return: 出现时为 `True`。
    副作用：无。
    """

    needle = _normalized(quote)
    if not needle:
        return False
    for unit in units:
        if self_only and not unit.is_self:
            continue
        if needle in _normalized(unit.quote_body):
            return True
    return False


# 角色占位符：学习侧把「只在这次成立的人和物」写成〈…〉。校验据此判断「说法」
# 是否由原话换出；只匹配成对的〈…〉，落单的「〈」会留在文字段里按原话校验。
_ROLE_PATTERN = re.compile(r'〈[^〈〉]*〉')


def _has_role_placeholder(style: str) -> bool:
    """判断说法里是否至少有一个成对的〈角色〉占位。

    :param style: 模型给出的说法文本（已剥前导词）。
    :return: 至少有一个〈…〉时为 `True`。
    副作用：无。
    """

    return _ROLE_PATTERN.search(style) is not None


def _segments_outside_roles(style: str) -> List[str]:
    """按〈角色〉切出说法剩下的文字段，返回去空白后的非空段（保持原顺序）。

    :param style: 模型给出的说法文本（已剥前导词）。
    :return: 角色占位之外的文字段列表；相邻占位之间可能产生空串，已剔除。
    副作用：无。
    """

    return [segment.strip() for segment in _ROLE_PATTERN.split(style) if segment.strip()]


def _contains_name(text: str, names: Sequence[str]) -> bool:
    """判断文本里是否出现 @ 或名字集合中的任一名字（不区分大小写）。

    :param text: 待检查的 style 或 situation。
    :param names: 需要拦截的名字；由调用方保证已做过长度过滤（在场者只收两字
        及以上），本函数不再判断长短。
    :return: 命中 @ 或任一名字时为 `True`。
    副作用：无。
    """

    if '@' in text:
        return True
    folded = text.casefold()
    for name in names:
        candidate = name.strip().casefold()
        if candidate and candidate in folded:
            return True
    return False


def _statement_follows_quote(style: str, quote: str) -> bool:
    """判断「说法」是否由原话换出：去掉〈角色〉后的文字段按顺序都在原话里。

    只比较去掉空白后的字符串；只含标点的段（例如两个角色占位之间的逗号）不
    携带原话内容，跳过不判。顺序要求防止把原话里的几段文字前后调换后拼成新句。

    :param style: 模型给出的说法文本（已剥前导词）。
    :param quote: 已确认是来源单元原样片段的那段话。
    :return: 每个非纯标点文字段都能按先后顺序在原话里找到时为 `True`。
    副作用：无。
    """

    quote_normalized = _normalized(quote)
    position = 0
    for segment in _segments_outside_roles(style):
        normalized = _normalized(segment)
        if not normalized:
            continue
        if all(char in _TRIM_PUNCTUATION for char in normalized):
            continue
        found = quote_normalized.find(normalized, position)
        if found < 0:
            return False
        position = found + len(normalized)
    return True


def validate_pairs(
    pairs: Sequence[ExpressionPair],
    *,
    units: Sequence[LearningUnit],
    names: Sequence[str] = (),
) -> Tuple[List[ExpressionPair], Dict[str, int]]:
    """按来源、复读、抄写与「什么值得学」规则校验候选，并按原因码统计拦截数。

    规则按序判定，单条只记第一个命中的原因（原因码见 :data:`DROP_REASON_LABELS`）：

    - `self`：来源不是本批群友单元（指向前文、指向她、越界）。
    - `quote`：原话去空白后为空，或不是来源单元可摘录正文的原样片段。
    - `echo_kind`：`kind = 复读学舌`。
    - `echo`：`kind = 说法` 且原话在更早单元（任何人，含她与前文）里出现过。
    - `echo_self`：`kind = 固定说法` 且原话在她更早的单元里出现过。
    - `copy`：`kind = 说法` 且 `style` 与原话去空白、去首尾标点后相同。
    - `empty`：`reading`、`situation`、`style` 任一为空（style 已剥前缀）。
    - `fixed_mismatch`：`kind = 固定说法`，但 style 与原话去空白、去首尾标点后
      不同，或 style 带「〈」——固定说法只收原样口头说法，改写或换角色都说明
      模型在借「固定说法」当放行通道。
    - `invented`：`kind = 说法` 但 style 不带〈角色〉，或去掉〈角色〉后剩下的
      文字段没有按先后顺序全部出现在原话里——后者覆盖「把另一句话写成说法」。
    - `name`：style 或 situation 含「@」、含名字集合里的名字（她的名字/别名/
      对主人的称呼，以及两位及以上的在场者显示名）。quote 是原话，不受此限。
    - `length`：`situation` 超过 60、`style` 超过 80、`reading` 超过 200
      或 `quote` 超过 80 字符（单位均为字符，含标点）。
    - `example`：`style` 与提示词示例里的说法相同（去空白后比较）。
    - `limit`：超出单批 :data:`MAX_PAIRS_PER_BATCH` 条上限。

    :param pairs: :func:`parse_learning` 校验过结构的候选列表。
    :param units: :func:`render_learning_corpus` 产出的全部单元（前文在前）。
    :param names: 不得出现在 style/situation 里的名字；调用方负责把她的名字族
        与两位及以上的在场者显示名合进来，单字名不传（避免误伤普通用字）。
    :return: `(可落库列表, 原因码计数)`；可落库列表保持原顺序，situation、
        style、reading、quote 已裁掉两端空白，style 已剥前导词；计数只含
        出现过至少一次的原因码。
    副作用：无。
    """

    accepted: List[ExpressionPair] = []
    dropped: Dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for pair in pairs:
        unit_index = pair.source - 1
        if unit_index < 0 or unit_index >= len(units):
            drop('self')
            continue
        unit = units[unit_index]
        # 前文与她的单元都只作上下文：前者是已经学过的历史，后者是自举闭环的源头。
        if unit.is_lead or unit.is_self:
            drop('self')
            continue
        quote = pair.quote.strip()
        # 原样片段判定：不做空白归一，也允许跨「／」匹配同一单元里连续的两条。
        if not quote or pair.quote not in unit.quote_body:
            drop('quote')
            continue
        if pair.kind == KIND_ECHO:
            drop('echo_kind')
            continue
        earlier = units[:unit_index]
        if pair.kind == KIND_STATEMENT and _appears_in(earlier, quote, self_only=False):
            drop('echo')
            continue
        if pair.kind == KIND_FIXED and _appears_in(earlier, quote, self_only=True):
            drop('echo_self')
            continue
        style = strip_style_prefix(pair.style)
        if (
            pair.kind == KIND_STATEMENT
            and _trimmed_punctuation(_normalized(style)) == _trimmed_punctuation(_normalized(quote))
        ):
            drop('copy')
            continue
        reading = pair.reading.strip()
        situation = pair.situation.strip()
        if not reading or not situation or not style:
            drop('empty')
            continue
        # 固定说法只能原样引用原话：改写或带角色占位都说明不是「本来就是口头
        # 说法」，而是普通句子借固定说法当放行通道。
        if pair.kind == KIND_FIXED and (
            '〈' in style
            or _trimmed_punctuation(_normalized(style))
            != _trimmed_punctuation(_normalized(quote))
        ):
            drop('fixed_mismatch')
            continue
        # 说法必须由原话换出：至少一个〈角色〉，且角色之外的文字按序出自原话。
        if pair.kind == KIND_STATEMENT and (
            not _has_role_placeholder(style)
            or not _statement_follows_quote(style, quote)
        ):
            drop('invented')
            continue
        if _contains_name(style, names) or _contains_name(situation, names):
            drop('name')
            continue
        if (
            len(situation) > MAX_SITUATION_CHARS
            or len(style) > MAX_STYLE_CHARS
            or len(reading) > MAX_READING_CHARS
            or len(quote) > MAX_QUOTE_CHARS
        ):
            drop('length')
            continue
        normalized_style = _normalized(style)
        if any(normalized_style == _normalized(example) for example in PROMPT_EXAMPLE_STYLES):
            drop('example')
            continue
        if len(accepted) >= MAX_PAIRS_PER_BATCH:
            drop('limit')
            continue
        accepted.append(ExpressionPair(
            source=pair.source,
            quote=quote,
            reading=reading,
            kind=pair.kind,
            situation=situation,
            style=style,
        ))
    return accepted, dropped


async def learn_expressions(
    provider: LlmProvider,
    *,
    bot_name: str,
    dialogue: str,
    temperature: float,
    max_tokens: Optional[int],
) -> Optional[List[ExpressionPair]]:
    """请求模型从一段单元化语料里找出值得以后再用的说法。

    :param provider: 提供流式文本输出的模型客户端（复用 memory 任务槽）。
    :param bot_name: Bot 展示名，进系统提示词；语料里也用它标注她自己的单元。
    :param dialogue: :func:`render_learning_corpus` 产出的逐行语料，含她的发言
        作为上下文。
    :param temperature: 采样温度。
    :param max_tokens: 输出上限；`None` 表示由 provider 决定。
    :return: 解析通过的候选列表；对话过短返回空列表（合法「没东西可学」），
        模型调用失败或输出不合契约时返回 `None`。
    副作用：发起一次流式模型请求并记录 `llm_request` 观测事件，不写数据库。
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
) -> Tuple[int, int]:
    """把校验通过的候选写入 expressions 表，区分新增与冲突加一。

    命中 `UNIQUE(situation, style, stream_id)` 时只把 `learn_count` 加一并
    用本次的 `reading`、`quote` 覆盖旧值；`checked`、`use_count`、
    `last_used_at`、`situation`、`style`、`source`、`created_at`
    一律不改：前几项是人工审核与使用证据，后两项是条目身份。新行 `checked = 0`
    （未复核），不进候选池：复核是放行闸门，仅人工确认的条目允许注入（见
    `agent/expression.py` 的 `fetch_expression_pool`）。

    先查后写不引入竞态：本函数是 expressions 表的唯一后台写入者，不同会话的
    并发由调用方按 stream 去重（同一 stream 同时只有一个学习任务在飞）。

    :param db: 当前库连接。
    :param stream_id: 目标 stream ID；候选池严格按会话隔离，学到的归属原会话。
    :param pairs: :func:`validate_pairs` 通过的候选列表。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: `(新增条数, 冲突加一条数)`。
    :raises sqlite3.Error: 写入失败时抛出。
    副作用：写入 expressions 并提交一次事务。
    """

    now = now if now is not None else current_time()
    inserted = 0
    increased = 0
    for pair in pairs:
        row = db.execute(
            'SELECT id FROM expressions'
            ' WHERE situation = ? AND style = ? AND stream_id = ?',
            (pair.situation, pair.style, stream_id),
        ).fetchone()
        if row is not None:
            db.execute(
                'UPDATE expressions SET learn_count = learn_count + 1,'
                ' reading = ?, quote = ? WHERE id = ?',
                (pair.reading, pair.quote, row[0]),
            )
            increased += 1
            continue
        db.execute(
            'INSERT INTO expressions'
            ' (situation, style, reading, quote, stream_id, source, created_at,'
            ' checked, learn_count, use_count)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, 0)',
            (
                pair.situation,
                pair.style,
                pair.reading,
                pair.quote,
                stream_id,
                LEARN_SOURCE,
                now,
            ),
        )
        inserted += 1
    db.commit()
    return inserted, increased


def eliminate_stale(db: sqlite3.Connection, now: Optional[int] = None) -> int:
    """按确定性规则淘汰表达方式，返回删除条数。

    淘汰范围只有本模块学来的行（`source = LEARN_SOURCE`）。迁移存量不自动
    删除，由人工在 WebUI 中清理；不得将存量纳入淘汰的依据见模块 docstring。

    在这个范围内两道规则按序执行：

    1. 保留期：`checked = 0` 且从未被选中（`last_used_at IS NULL`）且
       `created_at` 早于 `now - RETENTION_MS` 的行删除；
    2. 总量上限：学来的行仍超 :data:`MAX_LEARNED_ROWS` 时，在同样「未复核且
       从未被选中」的行里按 `created_at` 最旧继续删到达标。

    两道规则都只动 `checked = 0` 的行：人工确认（1）的永不淘汰；人工驳回（-1）
    是已判定的标记，删除后学习器可能再次提出同一内容。被选中过的行有使用
    证据，去留由使用频次决定，不在此删除。

    :param db: 当前库连接。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: 本次删除的总条数。
    :raises sqlite3.Error: 删除失败时抛出。
    副作用：删除 `expressions` 行并提交事务。
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
    batch: Optional[LearningBatch] = None,
    protected_names: Sequence[str] = (),
    temperature: float,
    max_tokens: Optional[int],
    now: Optional[int] = None,
) -> Optional[LearnReport]:
    """检查触发条件并完成一次学习与淘汰。

    调用方（`ChatService`）在回合收尾处调用；属后台任务，不放在回复关键
    路径上，失败不阻塞回合。同一 stream 的并发去重由调用方负责，
    形态与 `_maybe_extract_facts` 的内存集合一致。

    :param store: 记忆存储实例。
    :param provider: 学习任务的模型客户端（复用 memory 任务槽）。
    :param db: 当前库连接。
    :param stream_id: 目标 stream ID。
    :param participants: 前文与批次里出现过的在场者，只用于把群友发言渲染成
        带名字的行，学习不做人物归属判定；可以为空。
    :param bot_name: Bot 展示名。
    :param trigger_messages: 游标之后累积多少条消息才触发一次学习（按全部消息计）。
    :param batch: 可选的现成批次；调用方已经判过触发条件时传入，跳过本函数内部
        的触发检查与切批。`None` 表示由本函数自行判断并切批。
    :param protected_names: 不允许学进 style/situation 的名字族（Bot 主名、别名
        与对主人的称呼），由调用方按 `cfg.bot` 传入；在场者显示名由本函数从
        `participants` 里取两位及以上者合并进来，单字名不参与判定。
    :param temperature: 采样温度。
    :param max_tokens: 输出上限。
    :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
    :return: 学习对账；未达触发条件、没有群友消息、模型失败或输出不合契约时
        返回 `None`（模型失败不推进游标，下次重跑同一批）。
    :raises ValueError: 调用方传入的批次没有消息。
    :raises sqlite3.Error: 落库或淘汰失败时抛出。
    副作用：可能发起一次模型请求、写入 expressions、推进游标、删除过期条目，
        并发出观测事件。
    """

    now = now if now is not None else current_time()
    cursor = read_cursor(store, stream_id)
    if batch is None:
        if store.message_count_after(stream_id, cursor) < trigger_messages:
            return None
        collected = collect_learning_batch(store, stream_id, cursor)
        if collected is None:
            return None
        batch = collected
    if not batch.batch:
        raise ValueError('表达学习批次不能为空：调用方应传 collect_learning_batch 的结果')
    dialogue, units = render_learning_corpus(
        batch.lead,
        batch.batch,
        participants,
        bot_name,
    )
    message_count = len(batch.lead) + len(batch.batch)
    # 没有群友来源单元时学不到任何东西（理论上游标之后没有群友消息时 collect
    # 已返回 None，这里兜住调用方直接传入的批次）：不发起模型请求，游标照常推进。
    learnable = any(not unit.is_lead and not unit.is_self for unit in units)
    if not learnable:
        pairs: List[ExpressionPair] = []
    else:
        parsed = await learn_expressions(
            provider,
            bot_name=bot_name,
            dialogue=dialogue,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if parsed is None:
            # 模型故障或输出不合契约：不推进游标，下次重跑同一批，重复学习
            # 优于永久跳过该段对话。
            trace.emit('expression_learn_failed', streamId=stream_id, cursor=cursor)
            return None
        pairs = parsed
    # 名字保护：她的名字族由调用方按 cfg.bot 传入；在场者只收两位及以上的
    # 显示名，单字名（如「青」）会误伤「青草」这类普通用字。
    learning_names = [
        *protected_names,
        *(
            person.display_name.strip()
            for person in participants
            if len(person.display_name.strip()) >= 2
        ),
    ]
    accepted, dropped = validate_pairs(pairs, units=units, names=learning_names)
    learned, increased = persist_pairs(db, stream_id, accepted, now)
    advance_cursor(store, stream_id, batch.batch[-1].message_id)
    # 淘汰挂在学习之后同一次调用里，不另起后台任务。它清的是全表（各会话的过期
    # 条目一起清），由哪条会话触发并不重要。
    eliminated = eliminate_stale(db, now)
    remaining = int(db.execute('SELECT COUNT(*) FROM expressions').fetchone()[0])
    trace.emit(
        'expression_learned',
        streamId=stream_id,
        messageCount=message_count,
        units=len(units),
        candidates=len(pairs),
        learned=learned,
        increased=increased,
        dropped=dropped,
        eliminated=eliminated,
        remaining=remaining,
    )
    return LearnReport(
        message_count=message_count,
        units=len(units),
        candidates=len(pairs),
        learned=learned,
        increased=increased,
        dropped=dropped,
        eliminated=eliminated,
        remaining=remaining,
    )
