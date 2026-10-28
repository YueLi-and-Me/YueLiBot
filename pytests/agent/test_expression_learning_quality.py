"""补充轮学习标准用例（★1-★5、★7）：只学有辨识度的说法，说法必须出自原话。

本文件是补充任务的「什么值得学」收紧口径回归：
- 「固定说法」必须与原话一字不差、不带〈角色〉；
- 「说法」必须带〈角色〉，且去掉〈角色〉后的文字按顺序出现在原话里；
- 说法与情境里不得出现 @、她的名字/别名/对主人的称呼、两位及以上在场者显示名；
- 单批上限降到 3 条。

★6（渲染去除句末标点）在 pytests/llm/test_expression_select_context.py 中。
对话全部为虚构内容。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Tuple

import pytest

from src.core.agent.expression_learn import (
    KIND_FIXED,
    KIND_STATEMENT,
    MAX_PAIRS_PER_BATCH,
    ExpressionPair,
    collect_learning_batch,
    render_learning_corpus,
    run_learning,
    validate_pairs,
)
from src.core.agent.fact_extract import Participant
from src.core.memory.store import MemoryStore, StoredMessage

STREAM_ID = 1
NOW = 1_800_000_000_000
BOT_NAME = '月璃'

PEER_A = Participant(external_id='10001', display_name='阿青', person_id=11)
PEER_B = Participant(external_id='10002', display_name='小禾', person_id=12)
PARTICIPANTS = (PEER_A, PEER_B)


class _StubProvider:
    """返回空结果并记录请求的假 provider。"""

    async def stream(self, **_kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        yield {'text': '{"expressions": []}'}


def _msg(message_id: int, content: str, person_id: int | None = PEER_A.person_id) -> StoredMessage:
    """构造一条 StoredMessage；时间只用于排序，取固定基准。"""

    return StoredMessage(
        role='user' if person_id is not None else 'assistant',
        content=content,
        created_at=NOW + message_id,
        sender_person_id=person_id,
        message_id=message_id,
    )


def _units(*contents: str) -> Tuple[Any, ...]:
    """把若干条群友消息渲染成学习单元（一对一，不合并）。"""

    messages = tuple(_msg(index + 1, content) for index, content in enumerate(contents))
    _, units = render_learning_corpus((), messages, PARTICIPANTS, BOT_NAME)
    return tuple(units)


def _pair(
    source: int,
    quote: str,
    *,
    kind: str = KIND_STATEMENT,
    style: str,
    situation: str = '朋友抱怨一件糟心事、自己只是旁观时',
    reading: str = '在接对方的抱怨，用夸张的说法表示同情',
) -> ExpressionPair:
    """构造一条待校验候选；style/situation 由调用方按用例需要给出。"""

    return ExpressionPair(
        source=source,
        quote=quote,
        reading=reading,
        kind=kind,
        situation=situation,
        style=style,
    )


# ── ★1 固定说法必须与原话一字不差、不带〈〉 ──────────────────────────


def test_fixed_kind_requires_style_equal_quote_without_roles() -> None:
    """★1：固定说法只收原样口头说法；改写过的、带〈角色〉的都按 fixed_mismatch 拦。"""

    units = _units('太妙了')

    accepted, dropped = validate_pairs(
        [_pair(1, '太妙了', kind=KIND_FIXED, style='太妙了')],
        units=units,
    )
    assert len(accepted) == 1
    assert dropped == {}

    rewritten, dropped = validate_pairs(
        [_pair(1, '太妙了', kind=KIND_FIXED, style='太妙了呀')],
        units=units,
    )
    assert rewritten == []
    assert dropped == {'fixed_mismatch': 1}

    role_placeholder, dropped = validate_pairs(
        [_pair(1, '太妙了', kind=KIND_FIXED, style='太妙〈语气〉了')],
        units=units,
    )
    assert role_placeholder == []
    assert dropped == {'fixed_mismatch': 1}


# ── ★2 说法必须带〈角色〉 ────────────────────────────────────────────


def test_statement_without_role_placeholder_is_invented() -> None:
    """★2：kind=说法 的 style 不带〈角色〉，说明不是从原话换出来的，按 invented 拦。"""

    units = _units('这破表格我填了一下午')

    accepted, dropped = validate_pairs(
        [_pair(1, '这破表格我填了一下午', style='填了一下午')],
        units=units,
    )
    assert accepted == []
    assert dropped == {'invented': 1}


# ── ★3 说法的文字段必须出自原话 ──────────────────────────────────────


def test_statement_written_as_another_sentence_is_invented() -> None:
    """★3：复刻「原话一句、说法另写一句」的情况；文字段不在原话里按 invented 拦。"""

    units = _units('我先去把碗洗了，你慢慢吃')
    invented = _pair(
        1,
        '我先去把碗洗了，你慢慢吃',
        style='嗯〈手头的事忙完〉再聊',
    )

    accepted, dropped = validate_pairs([invented], units=units)
    assert accepted == []
    assert dropped == {'invented': 1}


# ── ★4 文字段按先后顺序出现在原话里 ──────────────────────────────────


def test_statement_segments_must_appear_in_quote_in_order() -> None:
    """★4：换角色后的文字段要按顺序都在原话里；顺序颠倒拦、顺序正确过、跨「／」同样按序。"""

    units = _units('食堂的糖醋里脊又没了，排队排了半小时')

    reordered, dropped = validate_pairs(
        [_pair(1, '食堂的糖醋里脊又没了，排队排了半小时',
               style='排队排了〈半小时〉，食堂的糖醋里脊〈又没了〉')],
        units=units,
    )
    assert reordered == []
    assert dropped == {'invented': 1}

    ordered, dropped = validate_pairs(
        [_pair(1, '食堂的糖醋里脊又没了，排队排了半小时',
               style='食堂的糖醋里脊〈又没了〉，排队排了〈半小时〉')],
        units=units,
    )
    assert len(ordered) == 1
    assert dropped == {}

    merged = _units('排队排了半小时／结果糖醋里脊没了')
    crossed, dropped = validate_pairs(
        [_pair(1, '排队排了半小时／结果糖醋里脊没了',
               style='排队排了〈多久〉／结果糖醋里脊〈怎么了〉')],
        units=merged,
    )
    assert len(crossed) == 1
    assert dropped == {}


# ── ★5 名字不进说法与情境 ────────────────────────────────────────────


def test_names_are_blocked_in_style_and_situation() -> None:
    """★5：@、她的名字/别名/对主人的称呼、两位以上在场者显示名，出现在 style/situation 即拦。"""

    units = _units('今天下雨了，青草都湿了')
    names = ('月璃', '璃璃', '阿青')
    quote = '今天下雨了，青草都湿了'

    at_units = _units('@阿青 你走路也太慢')
    at_style, dropped = validate_pairs(
        # 原话本身含 @，style 由原话换出后才会走到 name 判定。
        [_pair(1, '@阿青 你走路也太慢', style='@阿青 你走路也〈太慢〉')],
        units=at_units,
        names=names,
    )
    assert at_style == []
    assert dropped == {'name': 1}

    alias, dropped = validate_pairs(
        [_pair(1, quote, style='今天下雨了，青草〈都湿了〉', situation='璃璃问天气时')],
        units=units,
        names=names,
    )
    assert alias == []
    assert dropped == {'name': 1}

    peer_name, dropped = validate_pairs(
        # 名字放进〈角色〉内：先要通过 invented 判定，才能验到后面的 name 规则。
        [_pair(1, quote, style='今天下雨了，〈阿青的〉青草都湿了')],
        units=units,
        names=names,
    )
    assert peer_name == []
    assert dropped == {'name': 1}

    clean, dropped = validate_pairs(
        [_pair(1, quote, style='今天下雨了，青草〈都湿了〉')],
        units=units,
        names=names,
    )
    assert len(clean) == 1
    assert dropped == {}


@pytest.mark.asyncio
async def test_single_char_participant_name_does_not_block(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★5 补：在场者单字显示名不参与判定，避免误伤普通用字。"""

    import src.core.agent.expression_learn as module

    store = MemoryStore(db)
    single = Participant(external_id='20001', display_name='青', person_id=21)
    store.append_message(
        STREAM_ID, single.person_id, 'user', '今天下雨了，青草都湿了', NOW,
    )
    collected = collect_learning_batch(store, STREAM_ID, 0)
    assert collected is not None

    async def fake_learn(*_args: Any, **_kwargs: Any) -> List[ExpressionPair]:
        return [_pair(1, '今天下雨了，青草都湿了', style='今天下雨了，青草〈都湿了〉')]

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
        participants=(single,),
        bot_name=BOT_NAME,
        batch=collected,
        temperature=0.1,
        max_tokens=128,
        now=NOW,
    )

    assert report is not None
    # 单字名「青」不能拦住这条正常说法：它出现在 style 里属于普通用字。
    assert report.learned == 1
    assert report.dropped == {}


# ── ★7 单批上限为 3 ──────────────────────────────────────────────────


def test_at_most_three_pairs_per_batch() -> None:
    """★7：单批最多落库 3 条，超出的按 limit 单独计数。"""

    units = _units('这破表格我填了一下午')
    pairs = [
        _pair(1, '这破表格我填了一下午', style=f'这破表格〈第 {index} 种说法〉')
        for index in range(MAX_PAIRS_PER_BATCH + 2)
    ]

    accepted, dropped = validate_pairs(pairs, units=units)

    assert MAX_PAIRS_PER_BATCH == 3
    assert len(accepted) == MAX_PAIRS_PER_BATCH
    assert dropped == {'limit': 2}
