"""黑话学习的纯逻辑层：提取候选、累积证据、按阶梯阈值做三步推断。

本模块与 :mod:`src.core.services.jargon_learn` 分工：服务层负责调度与游标，
本层只做单批语料与单条词条的操作，可独立单测。

- 提取：模型从一批带行号的语料里挑选候选词条。候选不从 ``high_frequency_terms``
  取——那是分词产物，挑不出「典中典」这类短语；黑话常为中频词，按频次排序
  会系统性遗漏它们。
- 证据：词条每在一批语料里出现（模型挑中，或库内词条的子串命中）计
  ``sightings += 1``，每批每词至多一次。子串命中与
  :func:`src.core.agent.jargon.lookup_jargon` 同口径：归一化（去空白、转小写）
  后纯子串包含，机器段先剥离。常用词（如「码」）不会被提取模型选中，依赖该
  路径积累证据；缺少它时这些词条无法达到阈值、无法被重新判定。
- 推断：``sightings`` 到 4 / 8 / 25 / 100 各判一次，每次三步——带上下文推断
  含义、只看词推断含义、比较两次结果。两段含义一致判为普通词，有差异判为
  黑话；分两次独立推断再比较，结论不依赖单一提示表述。到 100 锁定不再推断。

落库纪律：新词一律 ``status='pending'``、``meaning=''``，推断判为黑话才
``confirmed``；判为普通词回到 ``pending`` 且保留原 ``meaning`` 不覆盖。无
人工审核环节，质量控制依赖证据阈值与比较判定。

学习写入不修改 :mod:`src.core.agent.jargon` 的匹配与打分口径，也不修改
:mod:`src.core.memory.high_frequency`；本模块是二者的使用者。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple
import json
import sqlite3

from src.core.agent.sub_agent import SubAgentCall, run_sub_agent
from src.core.common.clock import now as current_time
from src.core.common.logger import get_logger
from src.core.llm_models.openai import LlmError
from src.core.llm_models.protocol import LlmProvider, ResponseValidator
from src.core.memory.high_frequency import strip_machine_spans
from src.core.memory.store import MemoryStore, StoredMessage
from src.core.observe.events import emit
from src.core.prompts.registry import get_prompt, prompt_metadata

logger = get_logger(__name__)

# 学习游标的 meta 键。独立于摘要队列与事实抽取的判据：消息一旦被摘要归档
# 就从那些队列里消失，共用判据会让两个消费者相互消费对方的输入且不报错。
CURSOR_KEY = 'jargon_learn_cursor'

# 学习侧写入 jargon.source 的固定标识。须与历史迁移的「历史迁移:M-3」明显
# 不同，便于按 source 区分学习产物与迁移数据。
LEARN_SOURCE = '在线学习'

# 阶梯阈值：出现次数到每一档各推断一次。第一档过滤低频噪声词，不产生模型
# 调用；之后每档以更多证据重判一次；100 视为证据饱和，锁定不再推断。
SIGHTING_LADDER: Tuple[int, ...] = (4, 8, 25, 100)
COMPLETE_SIGHTINGS = 100

# 单批候选上限，即提取提示词的输出预算上界。
MAX_CANDIDATES_PER_BATCH = 30

# 每条词条保留的证据消息 id 上限。证据是审计线索与推断时的上下文索引，
# 保留最近若干条足以复现上下文。
MAX_EVIDENCE_IDS = 24

# 推断第①步取上下文时，每条证据消息向前后各带几条相邻消息、引用多少条证据、
# 以及整段上下文的行数上限。相邻消息提供证据所在的会话语境；Bot 自身的回复
# 也在此列，按 :func:`_speaker_label` 的约定标注为不采信。
EVIDENCE_CONTEXT_NEIGHBORS = 2
EVIDENCE_CONTEXT_MESSAGES = 6
EVIDENCE_CONTEXT_LINES = 12

# 一批语料里他人消息的正文总长低于该值时不发起提取模型调用，只做子串命中：
# 正文过短的批次无可提取内容。
MIN_USER_TEXT_CHARS = 120

# 模型响应长度上限（字符）。超长视为异常输出，判解析失败。
MAX_RESPONSE_CHARS = 4000


def _learn_switch_key(stream_id: int) -> str:
    """构造会话级学习开关的 meta 键名，与 ``jargon:use:{id}`` 同一约定。"""

    return f'jargon:learn:{stream_id}'


def jargon_learn_enabled(db: sqlite3.Connection, stream_id: int) -> bool:
    """读取会话级的黑话 learn 开关，缺省开。

    键位 ``jargon:learn:{stream_id}`` 预留，与
    :func:`src.core.agent.jargon.jargon_use_enabled` 的 use 键同一约定。

    :param db: 进程级 SQLite 连接。
    :param stream_id: 会话 ID。
    :return: 开关状态，缺省 ``True``。
    :raises sqlite3.Error: 读 ``meta`` 失败时抛出。
    副作用：无。
    """

    row = db.execute(
        'SELECT value FROM meta WHERE key = ?',
        (_learn_switch_key(stream_id),),
    ).fetchone()
    return row is None or str(row['value']) != '0'


# ---------------------------------------------------------------- 渲染与解析


def _normalize_key(term: str) -> str:
    """把词条归一化成匹配键：去空白、转小写，与 lookup_jargon 同口径。"""

    return term.strip().lower()


def _normalize_text(text: str) -> str:
    """把消息正文归一化成匹配语料：剥机器段、去空白、转小写。"""

    return strip_machine_spans(text.strip().lower())


def _speaker_label(role: str, sender_person_id: Optional[int], bot_name: str) -> str:
    """给语料行生成发言人标签；Bot 自身的发言带不可采信标记。"""

    if role == 'assistant':
        return f'{bot_name}（她自己说的，不采信）'
    return f'成员#{sender_person_id}' if sender_person_id is not None else '成员#?'


def render_corpus(
    batch: Sequence[StoredMessage],
    bot_name: str,
) -> Tuple[List[str], Dict[int, StoredMessage]]:
    """把一批消息渲染成带行号的语料行。

    :param batch: 按 ID 正序的消息批次，两种角色的消息都渲染；Bot 的发言
        带标记进语料，供提取与证据上下文识别其来源。
    :param bot_name: bot 展示名，用于标记 Bot 自身的行。
    :return: ``(语料行列表, 行号到消息的映射)``，行号从 1 起。
    副作用：无。
    """

    lines: List[str] = []
    by_line: Dict[int, StoredMessage] = {}
    for message in batch:
        content = (message.content or '').strip()
        if not content:
            continue
        by_line[len(lines) + 1] = message
        lines.append(
            f'L{len(lines) + 1} '
            f'{_speaker_label(message.role, message.sender_person_id, bot_name)}：'
            f'{content}'
        )
    return lines, by_line


def parse_candidates(raw: str) -> List[Tuple[str, int]]:
    """严格解析提取模型返回的候选数组。

    :param raw: 模型返回的完整 JSON 文本。
    :return: ``(词条原文, 行号)`` 列表，按模型返回顺序。
    :raises ValueError: 响应超长、JSON 结构不符、字段类型不对或数量超上限。
    副作用：无。
    """

    if len(raw) > MAX_RESPONSE_CHARS:
        raise ValueError(f'黑话提取输出超过 {MAX_RESPONSE_CHARS} 字符')
    try:
        payload = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError('黑话提取结果不是合法 JSON') from exc
    if not isinstance(payload, dict) or set(payload) != {'candidates'}:
        raise ValueError('黑话提取结果必须且只能包含 candidates 字段')
    candidates = payload['candidates']
    if not isinstance(candidates, list):
        raise ValueError('candidates 必须是数组')
    if len(candidates) > MAX_CANDIDATES_PER_BATCH:
        raise ValueError(
            f'提取了 {len(candidates)} 条候选，超过上限 {MAX_CANDIDATES_PER_BATCH}')
    picked: List[Tuple[str, int]] = []
    for item in candidates:
        if not isinstance(item, dict) or set(item) != {'term', 'line'}:
            raise ValueError(f'候选必须是 {{term, line}} 对象，收到 {item!r}')
        term, line = item['term'], item['line']
        if not isinstance(term, str) or not isinstance(line, int) or isinstance(line, bool):
            raise ValueError(f'候选字段类型不对，收到 {item!r}')
        picked.append((term, line))
    return picked


@dataclass
class MineOutcome:
    """一批语料提取完成后的记账结果，供 ``jargon_mined`` 事件与服务层判断。"""

    # 本批去重后的有效候选数。
    candidates: int = 0
    # 新插入的词条数。
    added: int = 0
    # 既有序条目被累加证据的数量（含子串命中，不含新增）。
    updated: int = 0
    # 库内词条在本批子串命中的数量。
    substring_hits: int = 0
    # 被丢弃的候选理由列表。
    dropped: List[str] = field(default_factory=list)
    # 本批是否发起了提取模型调用（语料太短时只做子串命中）。
    model_called: bool = False
    # 提取模型调用解析失败（本批未落库、游标不应推进）。
    failed: bool = False


def _load_known_names(db: sqlite3.Connection, bot_names: Sequence[str]) -> Set[str]:
    """汇总全部已知人名：平台显示名、各群名片与 bot 名字族。

    判据与 ``scripts/jargon_guard_names.py`` 一致：词条面与已知人名精确撞名的
    候选不入库；存量词条由 :func:`lock_name_collisions` 处理。

    :param db: 进程级 SQLite 连接。
    :param bot_names: bot 主名、别名与对用户的称呼。
    :return: 归一化（去空白、转小写）后的名字集合。
    :raises sqlite3.Error: 查询失败时抛出。
    副作用：无。
    """

    names = {name.strip().lower() for name in bot_names if name and name.strip()}
    for row in db.execute('SELECT display_name FROM identities'):
        name = str(row['display_name']).strip()
        if name:
            names.add(name.lower())
    for row in db.execute('SELECT group_card FROM group_memberships'):
        card = str(row['group_card']).strip()
        if card:
            names.add(card.lower())
    return names


def _has_word_char(term: str) -> bool:
    """判断词条是否含字母；纯标点数字串不算一个「词」。"""

    return any(char.isalpha() for char in term)


def _is_unmatchable_term(key: str) -> bool:
    """判断词条是否在查表层永远不可能被匹配。

    单个 ASCII 字母或数字是 :func:`lookup_jargon` 的既定噪声判据，学进来
    也永远进不了提示词，入库前直接丢弃。
    """

    return len(key) == 1 and key.isascii() and key.isalnum()


def _validate_pick(
    term: str,
    line: int,
    by_line: Dict[int, StoredMessage],
    known_names: Set[str],
    bot_names: Sequence[str],
) -> Optional[str]:
    """校验一条模型候选，返回丢弃理由；``None`` 表示通过。

    过滤规则逐条给理由：空词条、撞已知人名、含 bot 名字族、纯标点数字、
    永不匹配的单 ASCII 字符、行号越界、上下文为空。含 bot 名只查 ≥2 字的
    名字：单字别名做子串包含会产生大量误报。
    """

    key = _normalize_key(term)
    if not key:
        return '空词条'
    if key in known_names:
        return '撞已知人名'
    lowered_bot = [
        name.strip().lower() for name in bot_names
        if name and name.strip() and len(name.strip()) >= 2
    ]
    if any(name in key for name in lowered_bot):
        return '含bot名'
    if not _has_word_char(key):
        return '纯标点或数字'
    if _is_unmatchable_term(key):
        return '单个ASCII字符'
    if line not in by_line:
        return '行号越界'
    if not (by_line[line].content or '').strip():
        return '上下文为空'
    return None


def _load_scope_rows(db: sqlite3.Connection, stream_id: int) -> List[sqlite3.Row]:
    """取该会话专属与全局两份词条行，与查表的 scope 口径一致。

    :param db: 进程级 SQLite 连接。
    :param stream_id: 会话 ID。
    :return: 词条行列表。
    :raises sqlite3.Error: 查询失败时抛出。
    副作用：无。
    """

    return db.execute(
        'SELECT id, term, stream_id FROM jargon WHERE stream_id = ? OR stream_id IS NULL',
        (stream_id,),
    ).fetchall()


def _scan_known_terms(
    rows: Sequence[sqlite3.Row],
    user_messages: Sequence[Tuple[int, str]],
) -> Dict[int, int]:
    """子串扫描库内词条在本批他人消息里的命中。

    每行词条本批命中只记一次，证据取第一条包含它的消息。不滤 status：
    pending 词条积累证据与 confirmed 同等对待，判定为普通词的条目在证据
    继续增长后会被重新判定。

    :param rows: :func:`_load_scope_rows` 的词条行。
    :param user_messages: ``(消息 id, 原文)`` 序列，只含他人消息。
    :return: ``jargon.id`` 到首条证据消息 id 的映射。
    副作用：无。
    """

    normalized = [
        (message_id, _normalize_text(content))
        for message_id, content in user_messages
        if content and content.strip()
    ]
    if not normalized:
        return {}
    hits: Dict[int, int] = {}
    for row in rows:
        key = _normalize_key(str(row['term']))
        if not key:
            continue
        for message_id, text in normalized:
            if key in text:
                hits[int(row['id'])] = message_id
                break
    return hits


def _merge_evidence(raw: Optional[str], message_id: int) -> str:
    """把一条证据消息 id 并进既有 evidence_ids JSON 数组，保留最近若干条。

    :param raw: 库里原样的 evidence_ids 文本；``None`` 或空串按空表处理。
    :param message_id: 新证据消息 id。
    :return: 序列化后的 JSON 数组文本。
    :raises ValueError: 库内既有值不是整数数组（损坏数据）时抛出，
        本批失败而不静默忽略。
    副作用：无。
    """

    if raw is None or raw == '':
        existing: List[int] = []
    else:
        existing = _read_evidence_ids(raw)
    if message_id not in existing:
        existing.append(message_id)
    return json.dumps(existing[-MAX_EVIDENCE_IDS:], ensure_ascii=False)


def _read_evidence_ids(raw: Optional[str]) -> List[int]:
    """读出 evidence_ids 列的 id 数组；损坏值抛 :exc:`ValueError`，不静默忽略。"""

    if raw is None or raw == '':
        return []
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or any(not isinstance(v, int) for v in parsed):
        raise ValueError(f'evidence_ids 不是合法的 id 数组：{raw!r}')
    return parsed


async def mine_batch(
    db: sqlite3.Connection,
    provider: Optional[LlmProvider],
    *,
    stream_id: int,
    batch: Sequence[StoredMessage],
    bot_name: str,
    bot_names: Sequence[str],
    temperature: float,
    max_tokens: Optional[int],
    now: Optional[int] = None,
) -> MineOutcome:
    """对一批语料完成「提取 + 子串命中 + 落库」。

    提取模型解析失败时整批不落库并置 ``failed=True``，子串命中也不写：
    否则游标回退重跑同一批时会把同一段证据记两次。调用方（服务层）据此
    不推进游标，下轮重试同一批。

    :param db: 进程级 SQLite 连接。
    :param provider: 学习任务的模型客户端；``None`` 时只做子串命中。
    :param stream_id: 会话 ID。
    :param batch: 按 ID 正序的消息批次。
    :param bot_name: bot 展示名。
    :param bot_names: 名字守卫用的 bot 名字族（主名、别名、对用户的称呼）。
    :param temperature: 模型采样温度。
    :param max_tokens: 模型输出上限。
    :param now: 当前毫秒时间戳；省略时读时钟。
    :return: 本批记账结果。
    :raises sqlite3.Error: 落库失败时抛出。
    :raises ValueError: evidence_ids 既有值损坏时抛出。
    副作用：
        可能发起一次提取模型调用；在一个事务里写 ``jargon`` 表（新增与
        证据累加）；发出 ``jargon_mined`` 事件。
    """

    stamp = now if now is not None else current_time()
    outcome = MineOutcome()
    user_messages = [
        (m.message_id, m.content or '')
        for m in batch if m.role == 'user' and (m.content or '').strip()
    ]
    scope_rows = _load_scope_rows(db, stream_id)
    substring_hits = _scan_known_terms(scope_rows, user_messages)

    picks: List[Tuple[str, int]] = []
    by_line: Dict[int, StoredMessage] = {}
    corpus_chars = sum(len(content.strip()) for _, content in user_messages)
    if provider is not None and corpus_chars >= MIN_USER_TEXT_CHARS:
        lines, by_line = render_corpus(batch, bot_name)
        outcome.model_called = True
        raw = ''
        try:
            raw = await _call_model(
                provider,
                'jargon.mine',
                {
                    'bot_name': bot_name,
                    'corpus': '\n'.join(lines),
                    'max_candidates': str(MAX_CANDIDATES_PER_BATCH),
                },
                temperature=temperature,
                max_tokens=max_tokens,
                response_validator=_validate_candidates_response,
            )
            picks = parse_candidates(raw)
        except (LlmError, ValueError) as exc:
            if isinstance(exc, LlmError) and exc.kind != 'format':
                raise
            # 解析失败带完整信息落日志并放弃本批（含子串命中），游标不动，
            # 下轮重跑同一批。路由层把所有候选都判为格式不合格时走同一
            # 失败语义。
            logger.warning(
                'jargon_mine_parse_failed',
                streamId=stream_id,
                error=str(exc),
                textChars=len(raw),
                messages=len(batch),
            )
            outcome.failed = True
            return outcome

    known_names = _load_known_names(db, bot_names)
    existing_rows = {
        (_normalize_key(str(row['term'])), row['stream_id']): int(row['id'])
        for row in scope_rows
    }
    # 词条 id → 首条证据消息 id（本批每词至多 +1）。
    increments: Dict[int, int] = {}
    # 归一化词条键 → (原文, 证据消息 id)，本批新入库的词。
    fresh_terms: Dict[str, Tuple[str, int]] = {}
    seen_keys: Set[str] = set()
    for term, line in picks:
        reason = _validate_pick(term, line, by_line, known_names, bot_names)
        if reason is not None:
            outcome.dropped.append(reason)
            continue
        key = _normalize_key(term)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        outcome.candidates += 1
        message = by_line[line]
        # 查表口径的 scope 偏好：本会话专属词条优先，其次是全局词条，
        # 都没有才作为新的会话专属词条入库。
        target = existing_rows.get((key, stream_id))
        if target is None:
            target = existing_rows.get((key, None))
        if target is not None:
            increments[target] = message.message_id
        else:
            fresh_terms[key] = (term.strip(), message.message_id)

    for row_id, evidence_id in substring_hits.items():
        if row_id not in increments:
            increments[row_id] = evidence_id

    added = 0
    with db:
        for row_id, evidence_id in increments.items():
            row = db.execute(
                'SELECT evidence_ids FROM jargon WHERE id = ?', (row_id,),
            ).fetchone()
            if row is None:
                continue
            db.execute(
                '''UPDATE jargon
                   SET sightings = sightings + 1, evidence_ids = ?
                   WHERE id = ?''',
                (_merge_evidence(row['evidence_ids'], evidence_id), row_id),
            )
        for term, evidence_id in fresh_terms.values():
            db.execute(
                '''INSERT INTO jargon
                   (term, meaning, stream_id, status, hits, source, created_at,
                    sightings, evidence_ids, inferred_at_sightings)
                   VALUES (?, '', ?, 'pending', 0, ?, ?, 1, ?, 0)''',
                (term, stream_id, LEARN_SOURCE, stamp,
                 json.dumps([evidence_id], ensure_ascii=False)),
            )
            added += 1

    outcome.added = added
    outcome.updated = len(increments)
    outcome.substring_hits = len(substring_hits)
    emit(
        'jargon_mined',
        streamId=stream_id,
        messageCount=len(batch),
        userMessages=len(user_messages),
        candidates=outcome.candidates,
        added=outcome.added,
        updated=outcome.updated,
        dropped=len(outcome.dropped),
        reasons=_count_reasons(outcome.dropped),
        substringHits=len(substring_hits),
    )
    return outcome


def _count_reasons(reasons: Sequence[str]) -> Dict[str, int]:
    """把丢弃理由列表聚合成「理由 → 次数」，进事件载荷。"""

    counted: Dict[str, int] = {}
    for reason in reasons:
        counted[reason] = counted.get(reason, 0) + 1
    return counted


async def _call_model(
    provider: LlmProvider,
    template_id: str,
    values: Dict[str, str],
    *,
    temperature: float,
    max_tokens: Optional[int],
    response_validator: ResponseValidator | None = None,
) -> str:
    """渲染一份学习提示词并执行一次子代理调用，返回模型正文。"""

    prompt = get_prompt(template_id).render(**values)
    result = await run_sub_agent(SubAgentCall(
        task='jargon',
        provider=provider,
        messages=[{'role': 'system', 'content': prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={'type': 'json_object'},
        response_validator=response_validator,
        render_params={template_id: values},
        trace_extra=prompt_metadata(template_id, (template_id,)),
    ))
    return result.text


def _parse_meaning(raw: str, *, allow_insufficient: bool) -> Optional[str]:
    """严格解析一次含义推断的返回。

    :param raw: 模型返回的完整 JSON 文本。
    :param allow_insufficient: 是否接受 ``{"insufficient": true}`` 协议
        （只有第①步带上下文推断接受）。
    :return: 含义文本；``allow_insufficient`` 且模型声明信息不足时返回
        ``None`` 表示本次推断短路。
    :raises ValueError: 响应超长、结构不符或字段类型不对。
    副作用：无。
    """

    if len(raw) > MAX_RESPONSE_CHARS:
        raise ValueError(f'含义推断输出超过 {MAX_RESPONSE_CHARS} 字符')
    try:
        payload = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError('含义推断结果不是合法 JSON') from exc
    if not isinstance(payload, dict):
        raise ValueError('含义推断结果必须是 JSON 对象')
    if allow_insufficient and set(payload) == {'insufficient'}:
        if payload['insufficient'] is not True:
            raise ValueError(f'insufficient 只能为 true，收到 {payload!r}')
        return None
    if set(payload) != {'meaning'}:
        raise ValueError(f'含义推断结果必须且只能包含 meaning 字段，收到 {payload!r}')
    meaning = payload['meaning']
    if not isinstance(meaning, str) or not meaning.strip():
        raise ValueError('meaning 必须是非空字符串')
    return meaning.strip()


def _parse_verdict(raw: str) -> bool:
    """严格解析比较步的结论，返回两段含义是否相同。

    :raises ValueError: 响应超长、结构不符或字段不是布尔值。
    副作用：无。
    """

    if len(raw) > MAX_RESPONSE_CHARS:
        raise ValueError(f'比较结论输出超过 {MAX_RESPONSE_CHARS} 字符')
    try:
        payload = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError('比较结论不是合法 JSON') from exc
    if not isinstance(payload, dict) or set(payload) != {'same'}:
        raise ValueError(f'比较结论必须且只能包含 same 字段，收到 {payload!r}')
    same = payload['same']
    if not isinstance(same, bool):
        raise ValueError(f'same 必须是布尔值，收到 {payload!r}')
    return same


def _validate_candidates_response(raw: str) -> None:
    """验证候选提取完整响应，供模型路由在交付正文前切换不合格候选。"""

    parse_candidates(raw)


def _validate_context_meaning_response(raw: str) -> None:
    """验证带上下文含义响应，允许明确声明信息不足。"""

    _parse_meaning(raw, allow_insufficient=True)


def _validate_bare_meaning_response(raw: str) -> None:
    """验证通用词义响应，不接受信息不足分支。"""

    _parse_meaning(raw, allow_insufficient=False)


def _validate_verdict_response(raw: str) -> None:
    """验证含义比较结论响应。"""

    _parse_verdict(raw)


def _build_evidence_context(
    db: sqlite3.Connection,
    evidence_ids: Sequence[int],
    bot_name: str,
) -> str:
    """按证据消息 id 取上下文：每条证据带前后相邻消息，Bot 的发言打标。

    只取最近的 :data:`EVIDENCE_CONTEXT_MESSAGES` 条证据，限制上下文长度。
    全局词条的证据可能散在多个会话，相邻消息按各自证据所属的会话取，不跨
    会话拼上下文。Bot 的发言标注为不采信（见 :func:`_speaker_label`），
    避免以 Bot 的既有理解参与含义推断。

    :param db: 进程级 SQLite 连接。
    :param evidence_ids: 证据消息 id 序列（时间正序）。
    :param bot_name: bot 展示名。
    :return: 渲染好的上下文行文本，行间换行分隔。
    :raises sqlite3.Error: 查询失败时抛出。
    副作用：无。
    """

    picked: Dict[int, Tuple[str, Optional[int], str]] = {}
    for evidence_id in list(evidence_ids[-EVIDENCE_CONTEXT_MESSAGES:]):
        anchor = db.execute(
            'SELECT stream_id FROM messages WHERE id = ?', (evidence_id,),
        ).fetchone()
        if anchor is None or anchor['stream_id'] is None:
            continue
        stream_id = int(anchor['stream_id'])
        for row in db.execute(
            '''SELECT id, role, content, sender_person_id FROM messages
               WHERE stream_id = ? AND id BETWEEN ? AND ? ORDER BY id ASC''',
            (stream_id,
             evidence_id - EVIDENCE_CONTEXT_NEIGHBORS,
             evidence_id + EVIDENCE_CONTEXT_NEIGHBORS),
        ):
            content = (row['content'] or '').strip()
            if content:
                picked[int(row['id'])] = (
                    str(row['role']),
                    row['sender_person_id'],
                    content,
                )
    rendered = [
        f'{_speaker_label(role, sender, bot_name)}：{content}'
        for _, (role, sender, content) in sorted(picked.items())
    ]
    return '\n'.join(rendered[:EVIDENCE_CONTEXT_LINES])


def lock_name_collisions(
    db: sqlite3.Connection,
    bot_names: Sequence[str],
) -> int:
    """把与已知人名撞名的存量词条降级并永久锁定，返回处理条数。

    提取侧的名字守卫只拦新学的词条，不覆盖迁移带进来的存量。降级本身不阻止
    重新推断：词条仍会随出现次数达到阶梯阈值，而三步推断无法识别人名（群内
    称呼与字面含义的上下文差异真实存在），会被再次判定为黑话转回 confirmed。

    - 现象：``scripts/jargon_guard_names.py`` 降级过的名字在学习服务运行后
      陆续转回 confirmed。
    - 原因：守卫写在提取路径上，存量走的是推断路径，两条路径此前不共享判据。
    - 后果：不锁定则只能反复运行守卫脚本；注入侧的 ``protected_names`` 只
      保证不进提示词，词表与 WebUI 仍会把人名显示为已确认黑话。

    锁定手段复用阶梯机制：把 ``inferred_at_sightings`` 推到
    :data:`COMPLETE_SIGHTINGS`，:func:`select_inference_targets` 的
    ``inferred_at_sightings < 100`` 条件即把它们排除，不另加过滤层。

    :param db: 进程级 SQLite 连接。
    :param bot_names: bot 主名、别名与对用户的称呼，与提取侧同源。
    :return: 本次降级并锁定的条数；没有撞名词条时为 0。
    :raises sqlite3.Error: 查询或写回失败时抛出。
    副作用：写 ``status`` 与 ``inferred_at_sightings`` 并提交。
    """

    known_names = _load_known_names(db, bot_names)
    if not known_names:
        return 0
    # 词面归一化在 Python 侧做（与 _normalize_key 同口径），SQL 只按主键回写，
    # 不把名字集合拼进 SQL 文本。
    victims = [
        int(row['id'])
        for row in db.execute(
            "SELECT id, term FROM jargon WHERE inferred_at_sightings < ?",
            (COMPLETE_SIGHTINGS,),
        )
        if _normalize_key(str(row['term'])) in known_names
    ]
    if not victims:
        return 0
    with db:
        db.executemany(
            "UPDATE jargon SET status = 'pending', inferred_at_sightings = ?"
            ' WHERE id = ?',
            [(COMPLETE_SIGHTINGS, term_id) for term_id in victims],
        )
    logger.info('jargon_name_collisions_locked', count=len(victims))
    return len(victims)


def select_inference_targets(
    db: sqlite3.Connection,
    limit: int,
) -> List[sqlite3.Row]:
    """挑出该推断的词条：跨过了某个阶梯阈值且尚未在该阈值之上判过。

    判据是「存在阈值 t 满足 ``inferred_at_sightings < t ≤ sightings``」，
    展开成四档的静态 SQL。100 档即锁定档：``inferred_at_sightings >= 100``
    的词条直接排除。按证据数降序排序，证据最多的词条最先被重判。

    :param db: 进程级 SQLite 连接。
    :param limit: 最多返回的词条数（节流上限，见服务层）。
    :return: 词条行列表。
    :raises sqlite3.Error: 查询失败时抛出。
    副作用：无。
    """

    return db.execute(
        '''SELECT id, term, meaning, stream_id, status, sightings,
                  evidence_ids, inferred_at_sightings
           FROM jargon
           WHERE sightings >= 4 AND inferred_at_sightings < 100
             AND (inferred_at_sightings < 4
                  OR (inferred_at_sightings < 8 AND sightings >= 8)
                  OR (inferred_at_sightings < 25 AND sightings >= 25)
                  OR sightings >= 100)
           ORDER BY sightings DESC, id ASC
           LIMIT ?''',
        (limit,),
    ).fetchall()


async def infer_term(
    db: sqlite3.Connection,
    provider: LlmProvider,
    row: sqlite3.Row,
    *,
    bot_name: str,
    temperature: float,
    max_tokens: Optional[int],
) -> str:
    """对一个词条执行三步推断并写回，返回判定结果标签。

    三步：①带上下文推断群内含义（信息不足则短路）；②只看词推断通用含义；
    ③比较两段含义。任一步解析失败都不写库，写回仅在三步全部成功（或①明确
    声明信息不足）时发生。

    :param db: 进程级 SQLite 连接。
    :param provider: 学习任务的模型客户端。
    :param row: :func:`select_inference_targets` 返回的词条行。
    :param bot_name: bot 展示名。
    :param temperature: 模型采样温度。
    :param max_tokens: 模型输出上限。
    :return: ``'confirmed'`` / ``'normal_word'`` / ``'insufficient'``；
        模型输出解析失败时返回 ``'parse_failed'``（不写库）。
    :raises sqlite3.Error: 写回失败时抛出。
    副作用：
        至多三次模型调用；写回 ``status`` / ``meaning`` /
        ``inferred_at_sightings`` 并提交；发出 ``jargon_inferred`` 事件。
    """

    term = str(row['term'])
    stream_id = row['stream_id']
    sightings = int(row['sightings'])
    context = _build_evidence_context(
        db, _read_evidence_ids(row['evidence_ids']), bot_name)
    previous = str(row['meaning'] or '').strip()
    previous_block = (
        f'（此前记录过的释义，仅供参考、可以推翻：{previous}）' if previous else ''
    )
    raw_context = ''
    try:
        raw_context = await _call_model(
            provider, 'jargon.meaning.context',
            {'term': term, 'context': context, 'previous_meaning': previous_block},
            temperature=temperature, max_tokens=max_tokens,
            response_validator=_validate_context_meaning_response,
        )
        context_meaning = _parse_meaning(raw_context, allow_insufficient=True)
    except (LlmError, ValueError) as exc:
        if isinstance(exc, LlmError) and exc.kind != 'format':
            raise
        logger.warning(
            'jargon_infer_parse_failed', term=term, step='context',
            error=str(exc), textChars=len(raw_context))
        return 'parse_failed'
    if context_meaning is None:
        # 信息不足：立即结束本次推断，不做②③，但要把 inferred_at_sightings
        # 推到当前值，否则同一阈值会被反复尝试，浪费模型调用。
        with db:
            db.execute(
                'UPDATE jargon SET inferred_at_sightings = ? WHERE id = ?',
                (sightings, row['id']),
            )
        _emit_inference(term, stream_id, sightings, 'insufficient', '', '')
        return 'insufficient'

    raw_bare = ''
    try:
        raw_bare = await _call_model(
            provider, 'jargon.meaning.bare',
            {'term': term},
            temperature=temperature, max_tokens=max_tokens,
            response_validator=_validate_bare_meaning_response,
        )
        bare_meaning = _parse_meaning(raw_bare, allow_insufficient=False)
    except (LlmError, ValueError) as exc:
        if isinstance(exc, LlmError) and exc.kind != 'format':
            raise
        logger.warning(
            'jargon_infer_parse_failed', term=term, step='bare',
            error=str(exc), textChars=len(raw_bare))
        return 'parse_failed'

    raw_verdict = ''
    try:
        raw_verdict = await _call_model(
            provider, 'jargon.compare',
            {
                'term': term,
                'context_meaning': context_meaning,
                'bare_meaning': bare_meaning,
            },
            temperature=temperature, max_tokens=max_tokens,
            response_validator=_validate_verdict_response,
        )
        same = _parse_verdict(raw_verdict)
    except (LlmError, ValueError) as exc:
        if isinstance(exc, LlmError) and exc.kind != 'format':
            raise
        logger.warning(
            'jargon_infer_parse_failed', term=term, step='compare',
            error=str(exc), textChars=len(raw_verdict))
        return 'parse_failed'

    if same:
        # 普通词：回 pending，保留上一次的 meaning 不覆盖。此前释义可能来自
        # 更早、证据更足的推断，证据继续增长后仍会重判。
        with db:
            db.execute(
                '''UPDATE jargon
                   SET status = 'pending', inferred_at_sightings = ?
                   WHERE id = ?''',
                (sightings, row['id']),
            )
        _emit_inference(term, stream_id, sightings, 'normal_word',
                        context_meaning, bare_meaning)
        return 'normal_word'
    with db:
        db.execute(
            '''UPDATE jargon
               SET status = 'confirmed', meaning = ?, inferred_at_sightings = ?
               WHERE id = ?''',
            (context_meaning, sightings, row['id']),
        )
    _emit_inference(term, stream_id, sightings, 'confirmed',
                    context_meaning, bare_meaning)
    return 'confirmed'


def _emit_inference(
    term: str,
    stream_id: Optional[int],
    sightings: int,
    result: str,
    context_meaning: str,
    bare_meaning: str,
) -> None:
    """发出一条 ``jargon_inferred`` 事件，载荷带齐三步的可见结果。"""

    emit(
        'jargon_inferred',
        streamId=stream_id,
        term=term,
        sightings=sightings,
        result=result,
        contextMeaning=context_meaning,
        bareMeaning=bare_meaning,
    )


def read_cursor(store: MemoryStore, stream_id: int) -> int:
    """读取某个 stream 的学习游标；从未学过时返回 ``0``。

    :param store: 记忆存储实例。
    :param stream_id: 目标 stream ID。
    :return: 已学到的最后一条消息 ID。
    副作用：只读 ``meta`` 表。
    """

    raw = store.read_json(CURSOR_KEY, {})
    if not isinstance(raw, dict):
        return 0
    value = raw.get(str(stream_id))
    return int(value) if isinstance(value, int) else 0


def advance_cursor(store: MemoryStore, stream_id: int, message_id: int) -> None:
    """把某个 stream 的学习游标推进到指定消息（整批成功后才调用）。

    :param store: 记忆存储实例。
    :param stream_id: 目标 stream ID。
    :param message_id: 本批最后一条消息的 ID。
    :return: 无返回值。
    副作用：写入 ``meta`` 表并提交。
    """

    raw = store.read_json(CURSOR_KEY, {})
    cursor: Dict[str, int] = dict(raw) if isinstance(raw, dict) else {}
    cursor[str(stream_id)] = int(message_id)
    store.write_json(CURSOR_KEY, cursor)
