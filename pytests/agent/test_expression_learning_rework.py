"""表达学习的语料单元、六字段解析校验与落库行为回归（★1-★11）。

对话全部为虚构内容，只复刻真实事故的结构：同一人连发、接她的话、原样复读、
学她说过的固定说法、整句照抄、[回复 …] 前缀。不出现真实群聊原句。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Tuple

import pytest

from src.core.agent.expression_learn import (
    BATCH_PEER_MESSAGES,
    DROP_REASON_LABELS,
    KIND_ECHO,
    KIND_FIXED,
    KIND_STATEMENT,
    LEAD_CONTEXT_MESSAGES,
    LEARN_SOURCE,
    MAX_PAIRS_PER_BATCH,
    PROMPT_EXAMPLE_STYLES,
    ExpressionPair,
    collect_learning_batch,
    parse_learning,
    persist_pairs,
    read_cursor,
    render_learning_corpus,
    run_learning,
    validate_pairs,
)
from src.core.agent.fact_extract import Participant
from src.core.memory.store import (
    MemoryStore,
    StoredMessage,
    format_assistant_reaction_action,
)
from src.core.prompts.registry import get_prompt

STREAM_ID = 1
NOW = 1_800_000_000_000
BOT_NAME = '月璃'

PEER_A = Participant(external_id='10001', display_name='阿青', person_id=11)
PEER_B = Participant(external_id='10002', display_name='小禾', person_id=12)
PEER_C = Participant(external_id='10003', display_name='阿岩', person_id=13)
PARTICIPANTS = (PEER_A, PEER_B, PEER_C)


class _StubProvider:
    """按预设文本回放一次模型流，并记录收到的请求。"""

    def __init__(self, text: str = '{"expressions": []}') -> None:
        self.text = text
        self.requests: List[List[Dict[str, Any]]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        self.requests.append(kwargs['messages'])
        yield {'text': self.text}


def _msg(
    message_id: int,
    role: str,
    content: str,
    person_id: int | None = None,
) -> StoredMessage:
    """构造一条 StoredMessage；created_at 只用于时间排序，取固定基准即可。"""

    return StoredMessage(
        role=role,
        content=content,
        created_at=NOW + message_id,
        sender_person_id=person_id,
        message_id=message_id,
    )


def _pair(
    source: int,
    quote: str,
    *,
    reading: str = '这句在接上一个人的抱怨，用夸张的说法表示同情',
    kind: str = KIND_STATEMENT,
    situation: str = '朋友抱怨做重复劳动白费工夫、自己只是旁观时',
    style: str = '隔着屏幕都替你〈感受〉',
) -> ExpressionPair:
    """构造一条结构合法的候选；默认值都能通过校验。"""

    return ExpressionPair(
        source=source,
        quote=quote,
        reading=reading,
        kind=kind,
        situation=situation,
        style=style,
    )


def _seed_expression(
    db: Any,
    *,
    situation: str,
    style: str,
    checked: int,
    learn_count: int,
    reading: str = '',
    quote: str = '',
    use_count: int = 0,
    source: str = '迁移存量',
    created_at: int = 0,
) -> int:
    """直接种一行表达；落库用例需要绕开学习流程精确控制既有状态。"""

    cursor = db.execute(
        'INSERT INTO expressions'
        ' (situation, style, stream_id, use_count, source, created_at,'
        '  checked, learn_count, reading, quote)'
        ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            situation, style, STREAM_ID, use_count, source, created_at,
            checked, learn_count, reading, quote,
        ),
    )
    db.commit()
    return int(cursor.lastrowid)


def _seed_alternating(store: MemoryStore, peer_count: int) -> Tuple[List[int], List[int]]:
    """按「群友一条、她一条」交替写消息，返回群友与她各自的消息 ID。"""

    peer_ids: List[int] = []
    assistant_ids: List[int] = []
    for index in range(peer_count):
        peer_ids.append(store.append_message(
            STREAM_ID, PEER_A.person_id, 'user', f'群友的第 {index} 条消息', NOW + index * 2,
        ))
        assistant_ids.append(store.append_message(
            STREAM_ID, None, 'assistant', f'她的第 {index} 条回复', NOW + index * 2 + 1,
        ))
    return peer_ids, assistant_ids


# ── ★1 同一说话人连续合并 ────────────────────────────────────────────


def test_consecutive_same_speaker_merges_into_one_unit() -> None:
    """★1：连续同说话人合并成一条单元，中间有人发言才断开。"""

    dialogue, units = render_learning_corpus(
        (),
        (
            _msg(1, 'user', '这破表格我填了一下午', PEER_A.person_id),
            _msg(2, 'user', '填完又被打回来了', PEER_A.person_id),
            _msg(3, 'user', '你这也太惨了', PEER_B.person_id),
            _msg(4, 'user', '换我早不干了', PEER_B.person_id),
        ),
        PARTICIPANTS,
        BOT_NAME,
    )

    assert [unit.label for unit in units] == ['L1', 'L2']
    assert units[0].rendered == '这破表格我填了一下午／填完又被打回来了'
    assert units[0].message_ids == (1, 2)
    assert units[0].speaker == '[10001] 阿青'
    assert units[1].speaker == '[10002] 小禾'
    assert dialogue.splitlines() == [
        'L1[10001] 阿青：这破表格我填了一下午／填完又被打回来了',
        'L2[10002] 小禾：你这也太惨了／换我早不干了',
    ]


# ── ★2 她的发言保留为上下文、动作伪消息跳过 ───────────────────────────


def test_her_speech_stays_as_context_but_action_messages_are_skipped() -> None:
    """★2：她的真实发言进语料并标「她自己」，动作伪消息整条跳过。"""

    action = format_assistant_reaction_action(1, '无奈')
    dialogue, units = render_learning_corpus(
        (),
        (
            _msg(1, 'user', '我这儿键盘都要敲出火星子了', PEER_A.person_id),
            _msg(2, 'assistant', '<mood>烦躁</mood><say>那先歇一会儿</say><say>别硬扛着</say>'),
            _msg(3, 'assistant', action),
        ),
        PARTICIPANTS,
        BOT_NAME,
    )

    assert len(units) == 2
    her_unit = units[1]
    assert her_unit.is_self is True
    assert her_unit.speaker == f'{BOT_NAME}（她自己）'
    # 副作用标签必须剥掉；多气泡回复压成一行，否则续行没有说话人前缀。
    assert her_unit.rendered == '那先歇一会儿 别硬扛着'
    assert action not in dialogue
    assert '<mood>' not in dialogue


# ── ★3 前文标记与来源拦截 ────────────────────────────────────────────


def test_lead_units_are_marked_and_rejected_as_source() -> None:
    """★3：前文单元带「（前文）」标记，source 指向前文按来源不合规拦截。"""

    lead = (_msg(1, 'user', '我那个方案又被退回来了', PEER_A.person_id),)
    batch = (_msg(2, 'user', '我这周已经是第三回了', PEER_B.person_id),)
    dialogue, units = render_learning_corpus(lead, batch, PARTICIPANTS, BOT_NAME)

    assert dialogue.splitlines()[0] == 'L1（前文）[10001] 阿青：我那个方案又被退回来了'
    assert units[0].is_lead is True
    assert units[1].is_lead is False and units[1].label == 'L2'
    accepted, dropped = validate_pairs([_pair(1, '我那个方案又被退回来了')], units=units)
    assert accepted == []
    assert dropped == {'self': 1}


# ── ★4 指向她的单元 ─────────────────────────────────────────────────


def test_source_pointing_at_her_unit_is_rejected() -> None:
    """★4：source 指向「她自己」的单元按来源不合规拦截。"""

    _, units = render_learning_corpus(
        (),
        (
            _msg(1, 'assistant', '<say>这事得慢慢来</say>'),
            _msg(2, 'user', '行吧', PEER_A.person_id),
        ),
        PARTICIPANTS,
        BOT_NAME,
    )

    assert units[0].is_self is True
    accepted, dropped = validate_pairs([_pair(1, '这事得慢慢来')], units=units)
    assert accepted == []
    assert dropped == {'self': 1}


# ── ★5 群友原样学她更早一句 ──────────────────────────────────────────


def test_peer_repeating_her_earlier_line_is_echo() -> None:
    """★5：复刻「群友学她说话」的结构；kind=说法 且原话出现在她更早的单元里。

    旧做法把她的发言整段删掉，模型看不到被复读的原文，会把这句复读当成一条
    独立的新说法；新做法保留她的发言，用 echo 拦下。
    """

    _, units = render_learning_corpus(
        (),
        (
            _msg(1, 'assistant', '<say>这事得慢慢来</say>'),
            _msg(2, 'user', '你倒是挺淡定', PEER_A.person_id),
            _msg(3, 'user', '这事得慢慢来', PEER_B.person_id),
        ),
        PARTICIPANTS,
        BOT_NAME,
    )

    accepted, dropped = validate_pairs(
        [_pair(3, '这事得慢慢来', style='这事得〈处理方式〉慢慢来')],
        units=units,
    )
    assert accepted == []
    assert dropped == {'echo': 1}


# ── ★6 群友 B 原样复读群友 A ─────────────────────────────────────────


def test_peer_repeating_other_peer_is_echo() -> None:
    """★6：复刻「群友 A 先说、群友 B 原样复读」的结构。"""

    _, units = render_learning_corpus(
        (),
        (
            _msg(1, 'user', '你哪天改走养生路线了', PEER_A.person_id),
            _msg(2, 'user', '你哪天改走养生路线了', PEER_B.person_id),
        ),
        PARTICIPANTS,
        BOT_NAME,
    )

    accepted, dropped = validate_pairs(
        [_pair(2, '你哪天改走养生路线了', style='你哪天改走〈新路线〉了')],
        units=units,
    )
    assert accepted == []
    assert dropped == {'echo': 1}


# ── ★7 固定说法：别人先说过不拦，她先说过要拦 ────────────────────────


def test_fixed_kind_repeated_by_peer_passes_but_repeated_by_her_is_echo_self() -> None:
    """★7：固定说法本来就不指向人和物，群友之间互相说是正常接话。

    只有「她先说过、群友又学说」的固定说法要拦：那是在学她的口癖。
    """

    _, units = render_learning_corpus(
        (),
        (
            _msg(1, 'user', '太妙了', PEER_A.person_id),
            _msg(2, 'user', '太妙了', PEER_B.person_id),
        ),
        PARTICIPANTS,
        BOT_NAME,
    )
    accepted, dropped = validate_pairs(
        [_pair(2, '太妙了', kind=KIND_FIXED, style='太妙了')],
        units=units,
    )
    assert len(accepted) == 1
    assert accepted[0].kind == KIND_FIXED
    assert dropped == {}

    _, units = render_learning_corpus(
        (),
        (
            _msg(1, 'assistant', '<say>太妙了</say>'),
            _msg(2, 'user', '太妙了', PEER_B.person_id),
        ),
        PARTICIPANTS,
        BOT_NAME,
    )
    accepted, dropped = validate_pairs(
        [_pair(2, '太妙了', kind=KIND_FIXED, style='太妙了')],
        units=units,
    )
    assert accepted == []
    assert dropped == {'echo_self': 1}


# ── ★8 整句照抄 ─────────────────────────────────────────────────────


def test_statement_copying_quote_verbatim_is_blocked_but_role_placeholder_passes() -> None:
    """★8：kind=说法 且 style 与原话相同（含剥旧前缀、去首尾标点）按照抄拦。"""

    _, units = render_learning_corpus(
        (),
        (_msg(1, 'user', '白准备一包干粮', PEER_A.person_id),),
        PARTICIPANTS,
        BOT_NAME,
    )

    copied, dropped = validate_pairs(
        [_pair(1, '白准备一包干粮', style='白准备一包干粮')],
        units=units,
    )
    assert copied == []
    assert dropped == {'copy': 1}

    prefixed, dropped = validate_pairs(
        [_pair(1, '白准备一包干粮', style='使用白准备一包干粮')],
        units=units,
    )
    assert prefixed == []
    assert dropped == {'copy': 1}

    punctuated, dropped = validate_pairs(
        [_pair(1, '白准备一包干粮', style='白准备一包干粮。')],
        units=units,
    )
    assert punctuated == []
    assert dropped == {'copy': 1}

    accepted, dropped = validate_pairs(
        [_pair(1, '白准备一包干粮', style='白准备〈那堆东西〉')],
        units=units,
    )
    assert len(accepted) == 1
    assert accepted[0].style == '白准备〈那堆东西〉'
    assert dropped == {}


# ── ★9 引用前缀不算来源正文 ──────────────────────────────────────────


def test_quote_must_be_verbatim_substring_and_reply_prefix_is_not_source() -> None:
    """★9：[回复 …] 前缀保留在渲染行里，但不进入可摘录正文。"""

    _, units = render_learning_corpus(
        (),
        (
            _msg(1, 'user', '[回复 阿青：那个方案又改了] 这破表格我填了一下午', PEER_B.person_id),
        ),
        PARTICIPANTS,
        BOT_NAME,
    )

    assert units[0].rendered.startswith('[回复 阿青：那个方案又改了]')
    assert units[0].quote_body == '这破表格我填了一下午'

    quoted, dropped = validate_pairs(
        [_pair(1, '那个方案又改了', style='那个方案〈又出问题〉')],
        units=units,
    )
    assert quoted == []
    assert dropped == {'quote': 1}

    accepted, dropped = validate_pairs(
        [_pair(1, '这破表格我填了一下午', style='这破表格〈手上的活〉')],
        units=units,
    )
    assert len(accepted) == 1
    assert dropped == {}


# ── ★10 批次口径与游标推进 ──────────────────────────────────────────


def test_collect_returns_none_without_peer_messages(db: Any) -> None:
    """游标之后没有群友消息：没有可学来源，不返回批次（游标不应推进）。"""

    store = MemoryStore(db)
    store.append_message(STREAM_ID, None, 'assistant', '她自己的发言')

    assert collect_learning_batch(store, STREAM_ID, 0) is None


def test_lead_context_takes_six_messages_before_batch(db: Any) -> None:
    """前文取批次首条之前的 6 条消息，按 ID 正序；不足 6 条时有多少取多少。"""

    store = MemoryStore(db)
    lead_ids = [
        store.append_message(STREAM_ID, PEER_A.person_id, 'user', f'前情第 {index} 条', NOW + index)
        for index in range(8)
    ]
    batch_ids = [
        store.append_message(STREAM_ID, PEER_B.person_id, 'user', f'本批第 {index} 条', NOW + 8 + index)
        for index in range(3)
    ]

    collected = collect_learning_batch(store, STREAM_ID, lead_ids[7])

    assert collected is not None
    assert [message.message_id for message in collected.lead] == lead_ids[2:]
    assert len(collected.lead) == LEAD_CONTEXT_MESSAGES
    assert [message.message_id for message in collected.batch] == batch_ids


@pytest.mark.asyncio
async def test_batch_stops_at_sixteen_peer_messages_and_cursor_advances(db: Any) -> None:
    """★10：数够 16 条群友消息为止，她的发言不占名额；游标推进到批末。"""

    store = MemoryStore(db)
    peer_ids, assistant_ids = _seed_alternating(store, 20)

    collected = collect_learning_batch(store, STREAM_ID, 0)

    assert collected is not None
    assert sum(1 for message in collected.batch if message.role == 'user') == BATCH_PEER_MESSAGES
    assert sum(1 for message in collected.batch if message.role == 'assistant') == BATCH_PEER_MESSAGES - 1
    # 批末是第 16 条群友消息：其后她的回复与群友发言都不进本批。
    assert collected.batch[-1].message_id == peer_ids[BATCH_PEER_MESSAGES - 1]
    assert len(collected.batch) == 2 * BATCH_PEER_MESSAGES - 1
    assert collected.lead == ()
    assert assistant_ids[BATCH_PEER_MESSAGES - 1] not in {
        message.message_id for message in collected.batch
    }

    provider = _StubProvider()
    report = await run_learning(
        store,
        provider,
        db,
        stream_id=STREAM_ID,
        participants=PARTICIPANTS,
        bot_name=BOT_NAME,
        batch=collected,
        temperature=0.1,
        max_tokens=128,
        now=NOW,
    )

    assert report is not None
    assert read_cursor(store, STREAM_ID) == peer_ids[BATCH_PEER_MESSAGES - 1]
    assert len(provider.requests) == 1


# ── ★11 冲突加一与审核状态不变 ───────────────────────────────────────


def test_conflict_increments_learn_count_without_touching_review_state(db: Any) -> None:
    """★11：命中唯一键时 learn_count+1、reading/quote 换新，checked 等一律不动。"""

    approved_id = _seed_expression(
        db,
        situation='旧情境甲',
        style='旧句式甲',
        checked=1,
        learn_count=3,
        reading='旧说明甲',
        quote='旧原话甲',
        use_count=7,
    )
    rejected_id = _seed_expression(
        db,
        situation='旧情境乙',
        style='旧句式乙',
        checked=-1,
        learn_count=1,
        reading='旧说明乙',
        quote='旧原话乙',
        use_count=2,
    )
    pairs = [
        ExpressionPair(1, '新原话甲', '新说明甲', KIND_STATEMENT, '旧情境甲', '旧句式甲'),
        ExpressionPair(1, '新原话乙', '新说明乙', KIND_STATEMENT, '旧情境乙', '旧句式乙'),
        ExpressionPair(1, '新原话丙', '新说明丙', KIND_STATEMENT, '新情境丙', '新句式丙'),
    ]

    inserted, increased = persist_pairs(db, STREAM_ID, pairs, now=NOW)

    assert (inserted, increased) == (1, 2)
    rows = {
        row['id']: row
        for row in db.execute('SELECT * FROM expressions ORDER BY id').fetchall()
    }
    approved = rows[approved_id]
    assert approved['learn_count'] == 4
    assert approved['reading'] == '新说明甲'
    assert approved['quote'] == '新原话甲'
    assert approved['checked'] == 1
    assert approved['use_count'] == 7
    assert approved['situation'] == '旧情境甲'
    assert approved['style'] == '旧句式甲'
    assert approved['source'] == '迁移存量'
    assert approved['created_at'] == 0

    rejected = rows[rejected_id]
    assert rejected['learn_count'] == 2
    assert rejected['checked'] == -1
    assert rejected['reading'] == '新说明乙'
    assert rejected['quote'] == '新原话乙'

    new_row = rows[max(rows)]
    assert new_row['situation'] == '新情境丙'
    assert new_row['style'] == '新句式丙'
    assert new_row['checked'] == 0
    assert new_row['learn_count'] == 1
    assert new_row['use_count'] == 0
    assert new_row['source'] == LEARN_SOURCE
    assert new_row['created_at'] == NOW
    assert new_row['reading'] == '新说明丙'
    assert new_row['quote'] == '新原话丙'


# ── 解析与原因码 ────────────────────────────────────────────────────


def test_parse_learning_accepts_code_fence_and_all_fields() -> None:
    raw = (
        '说明文字\n'
        '{"expressions": [{"source": 3, "quote": "这破表格我填了一下午",'
        ' "reading": "在接对方的抱怨", "kind": "说法", "situation": "朋友抱怨时",'
        ' "style": "这破表格〈手上的活〉"}]}\n'
    )
    pairs = parse_learning(raw)

    assert pairs == [ExpressionPair(
        source=3,
        quote='这破表格我填了一下午',
        reading='在接对方的抱怨',
        kind=KIND_STATEMENT,
        situation='朋友抱怨时',
        style='这破表格〈手上的活〉',
    )]


@pytest.mark.parametrize('raw', [
    '抱歉，我没有找到值得学的内容',
    '[]',
    '{}',
    '{"expressions": "不是数组"}',
    '{"expressions": [1]}',
    '{"expressions": [{"source": 0, "quote": "a", "reading": "b", "kind": "说法", "situation": "c", "style": "d"}]}',
    '{"expressions": [{"source": true, "quote": "a", "reading": "b", "kind": "说法", "situation": "c", "style": "d"}]}',
    '{"expressions": [{"source": "3", "quote": "a", "reading": "b", "kind": "说法", "situation": "c", "style": "d"}]}',
    '{"expressions": [{"source": 3, "quote": "a", "reading": "b", "kind": "瞎说", "situation": "c", "style": "d"}]}',
    '{"expressions": [{"source": 3, "quote": "a", "reading": "b", "kind": "说法", "situation": "c"}]}',
    '{"expressions": [{"source": 3, "quote": 1, "reading": "b", "kind": "说法", "situation": "c", "style": "d"}]}',
])
def test_parse_learning_drops_whole_batch_on_invalid_structure(raw: str) -> None:
    """结构非法整批丢弃：不做逐条挑拣，避免游标推进后无法重学。"""

    assert parse_learning(raw) is None


def test_validate_pairs_reports_each_reason_code() -> None:
    """每个原因码都能被单独触发，且单条只记第一个命中的原因。"""

    _, units = render_learning_corpus(
        (),
        (
            _msg(1, 'user', '这破表格我填了一下午', PEER_A.person_id),
            _msg(2, 'user', '行嘞', PEER_B.person_id),
        ),
        PARTICIPANTS,
        BOT_NAME,
    )
    # 单批上限之外的既有原因码；length 用例的 style 必须能通过「说法出自原话」
    # 与名字判定，才能在 length 这一步被拦下。
    long_style = '这破表格〈' + '因' * 80 + '〉我填了一下午'
    cases = [
        (_pair(1, '这破表格我填了一下午', reading=''), 'empty'),
        (_pair(1, '这破表格我填了一下午', situation=''), 'empty'),
        (_pair(1, '这破表格我填了一下午', style='使用'), 'empty'),
        (_pair(1, '这破表格我填了一下午', situation='情' * 61,
               style='这破表格〈手上的活〉'), 'length'),
        (_pair(1, '这破表格我填了一下午', style=long_style), 'length'),
        (_pair(1, '这破表格我填了一下午', reading='说' * 201,
               style='这破表格〈手上的活〉'), 'length'),
        (_pair(2, '行嘞', kind=KIND_FIXED, style='行嘞'), 'example'),
        (_pair(1, '这破表格我填了一下午', kind=KIND_ECHO), 'echo_kind'),
        (_pair(1, '这句话根本不在来源里'), 'quote'),
    ]
    for pair, expected in cases:
        accepted, dropped = validate_pairs([pair], units=units)
        assert accepted == [], (pair, dropped)
        assert dropped == {expected: 1}, (pair, dropped)


def test_validate_pairs_keeps_at_most_three_pairs_per_batch() -> None:
    """单批 3 条上限：超出部分按 limit 单独计数（补充任务把上限从 8 收到 3）。"""

    _, units = render_learning_corpus(
        (),
        (_msg(1, 'user', '这破表格我填了一下午', PEER_A.person_id),),
        PARTICIPANTS,
        BOT_NAME,
    )
    pairs = [
        _pair(1, '这破表格我填了一下午', style=f'这破表格〈第 {index} 种说法〉')
        for index in range(MAX_PAIRS_PER_BATCH + 2)
    ]

    accepted, dropped = validate_pairs(pairs, units=units)

    assert len(accepted) == MAX_PAIRS_PER_BATCH
    assert dropped == {'limit': 2}


def test_drop_reason_labels_cover_every_contract_reason_code() -> None:
    """原因码到中文名的映射与任务书表格一一对应，供日志展示使用。"""

    assert set(DROP_REASON_LABELS) == {
        'self', 'quote', 'echo_kind', 'echo', 'echo_self',
        'copy', 'empty', 'fixed_mismatch', 'invented', 'name',
        'length', 'example', 'limit',
    }


# ── 提示词与事件 ────────────────────────────────────────────────────


def test_prompt_examples_match_style_constants_and_relation_rules() -> None:
    """示例 style 必须等于代码常量（校验按常量拦抄示例），且两条关系规则保留。"""

    text = get_prompt('expression.learn').text

    for style in PROMPT_EXAMPLE_STYLES:
        assert style in text
    assert '{{bot_name}}' in text
    assert '不要学对人的称呼' in text
    assert '称呼代表两个人之间的关系，不是说话风格' in text


@pytest.mark.asyncio
async def test_run_learning_reports_units_increased_and_dropped(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """学习完成事件带上 units、increased、dropped，供控制台展示拦截明细。"""

    import src.core.agent.expression_learn as module

    store = MemoryStore(db)
    store.append_message(STREAM_ID, PEER_A.person_id, 'user', '这破表格我填了一下午', NOW)
    store.append_message(STREAM_ID, None, 'assistant', '那先歇一会儿', NOW + 1)
    collected = collect_learning_batch(store, STREAM_ID, 0)
    assert collected is not None
    _seed_expression(
        db,
        situation='旧情境',
        style='这破表格〈手上的活〉',
        checked=1,
        learn_count=1,
    )

    async def fake_learn(*_args: Any, **_kwargs: Any) -> List[ExpressionPair]:
        return [
            ExpressionPair(
                1, '这破表格我填了一下午', '在接对方的抱怨', KIND_STATEMENT,
                '旧情境', '这破表格〈手上的活〉',
            ),
            ExpressionPair(
                2, '那先歇一会儿', '她在劝对方先停下来', KIND_STATEMENT,
                '劝人休息时', '那先〈歇着〉',
            ),
        ]

    emitted: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(module, 'learn_expressions', fake_learn)
    monkeypatch.setattr(
        module.trace,
        'emit',
        lambda event, **fields: emitted.append((event, fields)),
    )

    report = await run_learning(
        store,
        _StubProvider(),
        db,
        stream_id=STREAM_ID,
        participants=PARTICIPANTS,
        bot_name=BOT_NAME,
        batch=collected,
        temperature=0.1,
        max_tokens=128,
        now=NOW,
    )

    assert report is not None
    assert report.units == 2
    assert report.candidates == 2
    assert report.learned == 0
    assert report.increased == 1
    assert report.dropped == {'self': 1}
    assert [event for event, _ in emitted] == ['expression_learned']
    fields = emitted[0][1]
    assert fields['units'] == 2
    assert fields['increased'] == 1
    assert fields['dropped'] == {'self': 1}
    assert fields['messageCount'] == report.message_count
