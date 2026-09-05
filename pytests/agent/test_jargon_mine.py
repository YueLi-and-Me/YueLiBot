"""黑话学习验收：提取解析、名字守卫、证据累计、阶梯挑选与三步推断。

对应 开发文档 jargon-learning.md（不随代码分发） 的核心机制（机检部分）：

- 候选解析严格协议（多字段/超长/越界一律拒收，不静默吞）。
- 名字守卫：撞已知人名、含 bot 名字族的候选直接丢弃不入库。
- 决定 4：库内词条在本批语料的子串命中同样 ``sightings += 1``，机器段剥离，
  她自己的发言不计入证据；每批每词至多 +1。
- 决定 3：新词一律 pending + 空 meaning + 固定 source；判为普通词回 pending
  且不覆盖 meaning；判为黑话才 confirmed 并写推断释义。
- 决定 5：信息不足短路但推进 inferred_at_sightings；解析失败整批不落库。
- 阶梯挑选：4/8/25/100 各档判一次，100 锁定。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

import pytest

from src.core.agent.jargon_mine import (
    LEARN_SOURCE,
    advance_cursor,
    infer_term,
    jargon_learn_enabled,
    mine_batch,
    parse_candidates,
    read_cursor,
    select_inference_targets,
)
from src.core.agent.sub_agent import SubAgentCall
from src.core.llm_models.openai import LlmError
from src.core.memory.store import MemoryStore, StoredMessage
from src.core.observe import events as observe_events


BOT_NAME = '月璃'
BOT_NAMES = ('月璃', '小璃', '璃宝')
STREAM_ID = 7


class _ScriptedProvider:
    """按剧本逐条回话的假模型客户端，记录每次请求供断言。"""

    def __init__(self, replies: Sequence[str]) -> None:
        self._replies = list(replies)
        self.requests: List[List[Dict[str, Any]]] = []

    async def stream(self, messages: list[dict], **_kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        self.requests.append([dict(message) for message in messages])
        yield {'text': self._replies.pop(0)}


class _FormatFailingProvider:
    """模拟路由层已经穷尽候选且均未通过响应格式校验。"""

    async def stream(
        self,
        messages: list[dict],
        **_kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        del messages
        raise LlmError('format', '所有候选的结构化输出都不合格')
        yield  # pragma: no cover


def _seed_stream(db, stream_id: int = STREAM_ID) -> None:
    db.execute(
        'INSERT INTO streams (id, platform, kind, external_id) VALUES (?, ?, ?, ?)',
        (stream_id, 'test', 'group', f'test-{stream_id}'),
    )
    db.commit()


def _seed_jargon(
    db,
    term: str,
    meaning: str = '',
    stream_id: Optional[int] = None,
    status: str = 'confirmed',
    sightings: int = 0,
    inferred_at: int = 0,
    evidence_ids: Optional[str] = None,
) -> int:
    cursor = db.execute(
        '''INSERT INTO jargon
           (term, meaning, stream_id, status, hits, source, created_at,
            sightings, evidence_ids, inferred_at_sightings)
           VALUES (?, ?, ?, ?, 0, 'w-test', ?, ?, ?, ?)''',
        (term, meaning, stream_id, status, int(time.time() * 1000),
         sightings, evidence_ids, inferred_at),
    )
    db.commit()
    return int(cursor.lastrowid)


def _row(db, term: str) -> sqlite3.Row:
    row = db.execute('SELECT * FROM jargon WHERE term = ?', (term,)).fetchone()
    assert row is not None, f'词条不存在：{term}'
    return row


def _msg(message_id: int, role: str, content: str, sender: Optional[int] = 1) -> StoredMessage:
    return StoredMessage(
        message_id=message_id, role=role, content=content,
        created_at=message_id, sender_person_id=sender if role == 'user' else None,
    )


def _insert_messages(db, stream_id: int, specs: Sequence[tuple[str, str]]) -> List[StoredMessage]:
    """按 (role, content) 落库一批消息并返回对应 StoredMessage 列表。"""

    result: List[StoredMessage] = []
    for role, content in specs:
        cursor = db.execute(
            'INSERT INTO messages (stream_id, role, content, created_at, sender_person_id)'
            " VALUES (?, ?, ?, ?, ?)",
            (stream_id, role, content, int(time.time() * 1000),
             1 if role == 'user' else None),
        )
        result.append(StoredMessage(
            message_id=int(cursor.lastrowid), role=role, content=content,
            created_at=0, sender_person_id=1 if role == 'user' else None,
        ))
    db.commit()
    return result


# ---------------------------------------------------------------- 候选解析


def test_m1_parse_candidates_accepts_strict_protocol() -> None:
    payload = '{"candidates": [{"term": "典中典", "line": 3}, {"term": "你币有了", "line": 9}]}'
    assert parse_candidates(payload) == [('典中典', 3), ('你币有了', 9)]


@pytest.mark.parametrize('raw', [
    '随便聊聊',
    '{"terms": []}',
    '{"candidates": {}}',
    '{"candidates": [{"term": "a", "line": true}]}',
    '{"candidates": [{"term": 1, "line": 1}]}',
    '{"candidates": [{"term": "a"}]}',
    json.dumps({'candidates': [{'term': f'词{i}', 'line': i} for i in range(31)]}),
])
def test_m1_parse_candidates_rejects_bad_protocol(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_candidates(raw)


def test_m1_parse_candidates_rejects_overlong_response() -> None:
    with pytest.raises(ValueError):
        parse_candidates('x' * 5000)


# ---------------------------------------------------------------- 提取与守卫


def test_m2_new_term_inserted_pending_with_source(db) -> None:
    """新词入库：pending、空释义、固定 source、sightings=1、证据可溯。"""

    _seed_stream(db)
    batch = _insert_messages(db, STREAM_ID, [
        ('user', '这波操作真是典中典，我都看笑了'),
        ('user', '典中典看多了，人都麻了，没什么感觉了'),
        ('user', '你们天天说这个词，我到现在都没搞懂什么意思'),
        ('user', '今天天气不错，中午出来吃饭，顺便聊聊这事'),
        ('user', '闲聊两句别的：今天整体节奏还不错，上午把该看的材料都看完了，下午准备把剩下的一点收尾工作做完，晚上打算早点休息，明天还有明天的事情，不用都赶在今天这一时半会儿里，慢慢来就行，不着急这一两天。'),
    ])
    provider = _ScriptedProvider([
        json.dumps({'candidates': [{'term': '典中典', 'line': 1}]}),
    ])
    outcome = asyncio.run(mine_batch(
        db, provider, stream_id=STREAM_ID, batch=batch,
        bot_name=BOT_NAME, bot_names=BOT_NAMES,
        temperature=0.1, max_tokens=64,
    ))
    assert outcome.failed is False
    assert outcome.candidates == 1
    assert outcome.added == 1
    row = _row(db, '典中典')
    assert row['status'] == 'pending'
    assert row['meaning'] == ''
    assert row['source'] == LEARN_SOURCE
    assert row['stream_id'] == STREAM_ID
    assert row['sightings'] == 1
    assert row['inferred_at_sightings'] == 0
    assert json.loads(row['evidence_ids']) == [batch[0].message_id]


def test_m2_known_person_name_candidate_discarded(db) -> None:
    """决定 6：词条面与已知人名精确撞名的候选直接丢弃，不入库。"""

    _seed_stream(db)
    db.execute(
        "INSERT INTO identities (person_id, platform, external_id, display_name)"
        " VALUES (1, 'test', '10001', '阿凯')",
    )
    db.commit()
    batch = _insert_messages(db, STREAM_ID, [
        ('user', '阿凯今天又迟到了，这已经是这个月第三次了'),
        ('user', '月璃酱你说是不是该说说他，大家都等着他开工呢'),
        ('user', '闲聊两句别的：今天整体节奏还不错，上午把该看的材料都看完了，下午准备把剩下的一点收尾工作做完，晚上打算早点休息，明天还有明天的事情，不用都赶在今天这一时半会儿里，慢慢来就行，不着急这一两天。'),
    ])
    provider = _ScriptedProvider([
        json.dumps({'candidates': [
            {'term': '阿凯', 'line': 1},
            {'term': '月璃酱', 'line': 1},
        ]}),
    ])
    outcome = asyncio.run(mine_batch(
        db, provider, stream_id=STREAM_ID, batch=batch,
        bot_name=BOT_NAME, bot_names=BOT_NAMES,
        temperature=0.1, max_tokens=64,
    ))
    assert outcome.added == 0
    assert outcome.dropped == ['撞已知人名', '含bot名']
    assert db.execute('SELECT COUNT(*) FROM jargon').fetchone()[0] == 0


def test_m2_line_out_of_range_discarded(db) -> None:
    _seed_stream(db)
    batch = _insert_messages(db, STREAM_ID, [
        ('user', '咕了一整天，什么也没干成，进度条一动不动'),
        ('user', '再这样下去这周的计划又要泡汤了，愁人'),
        ('user', '闲聊两句别的：今天整体节奏还不错，上午把该看的材料都看完了，下午准备把剩下的一点收尾工作做完，晚上打算早点休息，明天还有明天的事情，不用都赶在今天这一时半会儿里，慢慢来就行，不着急这一两天。'),
    ])
    provider = _ScriptedProvider([
        json.dumps({'candidates': [{'term': '咕', 'line': 99}]}),
    ])
    outcome = asyncio.run(mine_batch(
        db, provider, stream_id=STREAM_ID, batch=batch,
        bot_name=BOT_NAME, bot_names=BOT_NAMES,
        temperature=0.1, max_tokens=64,
    ))
    assert outcome.dropped == ['行号越界']
    assert outcome.added == 0


def test_m2_parse_failure_keeps_batch_unwritten(db) -> None:
    """决定 5 的失败面：解析失败整批不落库（含子串命中），事件不发。"""

    _seed_stream(db)
    _seed_jargon(db, '干饭', meaning='吃饭')
    batch = _insert_messages(db, STREAM_ID, [
        ('user', '中午干饭去了，食堂排队排了二十分钟才吃到'),
        ('user', '吃完回来困得不行，打算趴一会儿再继续干活'),
        ('user', '闲聊两句别的：今天整体节奏还不错，上午把该看的材料都看完了，下午准备把剩下的一点收尾工作做完，晚上打算早点休息，明天还有明天的事情，不用都赶在今天这一时半会儿里，慢慢来就行，不着急这一两天。'),
    ])
    provider = _ScriptedProvider(['这不是 JSON'])
    outcome = asyncio.run(mine_batch(
        db, provider, stream_id=STREAM_ID, batch=batch,
        bot_name=BOT_NAME, bot_names=BOT_NAMES,
        temperature=0.1, max_tokens=64,
    ))
    assert outcome.failed is True
    assert _row(db, '干饭')['sightings'] == 0


def test_m2_short_corpus_skips_model_but_counts_substring(db) -> None:
    """语料太短只做子串命中：不烧模型调用，证据照记。"""

    _seed_stream(db)
    _seed_jargon(db, '干饭', meaning='吃饭')
    batch = _insert_messages(db, STREAM_ID, [('user', '干饭')])
    outcome = asyncio.run(mine_batch(
        db, None, stream_id=STREAM_ID, batch=batch,
        bot_name=BOT_NAME, bot_names=BOT_NAMES,
        temperature=0.1, max_tokens=64,
    ))
    assert outcome.model_called is False
    assert outcome.substring_hits == 1
    assert _row(db, '干饭')['sightings'] == 1


# ---------------------------------------------------------------- 证据累计


def test_m3_substring_hit_counts_sightings_per_batch(db) -> None:
    """决定 4：库内词条子串命中 +1/批；两批 +2，证据追加并封顶。"""

    _seed_stream(db)
    _seed_jargon(db, '干饭', meaning='吃饭')
    first = _insert_messages(db, STREAM_ID, [('user', '走，干饭去')])
    second = _insert_messages(db, STREAM_ID, [('user', '干饭干饭'), ('user', '又干饭')])
    for batch in (first, second):
        outcome = asyncio.run(mine_batch(
            db, None, stream_id=STREAM_ID, batch=batch,
            bot_name=BOT_NAME, bot_names=BOT_NAMES,
            temperature=0.1, max_tokens=64,
        ))
        assert outcome.substring_hits == 1
    row = _row(db, '干饭')
    assert row['sightings'] == 2
    assert json.loads(row['evidence_ids']) == [
        first[0].message_id, second[0].message_id]


def test_m3_machine_spans_and_bot_messages_excluded(db) -> None:
    """机器段先剥离；她自己的发言不算群内证据。"""

    _seed_stream(db)
    _seed_jargon(db, '干饭', meaning='吃饭')
    batch = [
        _msg(101, 'user', '[图片：干饭.png]'),
        _msg(102, 'assistant', '我也要去干饭！'),
    ]
    outcome = asyncio.run(mine_batch(
        db, None, stream_id=STREAM_ID, batch=batch,
        bot_name=BOT_NAME, bot_names=BOT_NAMES,
        temperature=0.1, max_tokens=64,
    ))
    assert outcome.substring_hits == 0
    assert _row(db, '干饭')['sightings'] == 0


def test_m3_model_pick_of_existing_term_merges_once(db) -> None:
    """模型挑中已有词：合并证据 +1（不因重复挑中多加）。"""

    _seed_stream(db)
    _seed_jargon(db, '典中典', meaning='')
    batch = _insert_messages(db, STREAM_ID, [
        ('user', '典中典，又是这一出，每次都这个套路'),
        ('user', '太典中典了，看开头就能猜到结尾那种'),
        ('user', '这词最近在群里用得越来越多了'),
        ('user', '闲聊两句别的：今天整体节奏还不错，上午把该看的材料都看完了，下午准备把剩下的一点收尾工作做完，晚上打算早点休息，明天还有明天的事情，不用都赶在今天这一时半会儿里，慢慢来就行，不着急这一两天。'),
    ])
    provider = _ScriptedProvider([
        json.dumps({'candidates': [
            {'term': '典中典', 'line': 1},
            {'term': '典中典', 'line': 2},
        ]}),
    ])
    outcome = asyncio.run(mine_batch(
        db, provider, stream_id=STREAM_ID, batch=batch,
        bot_name=BOT_NAME, bot_names=BOT_NAMES,
        temperature=0.1, max_tokens=64,
    ))
    assert outcome.candidates == 1
    assert outcome.updated == 1
    assert _row(db, '典中典')['sightings'] == 1


def test_m3_global_term_hit_from_any_stream(db) -> None:
    """全局词条在任何会话出现都攒证据（存量自愈的入口）。"""

    _seed_stream(db)
    _seed_jargon(db, '摸鱼', meaning='偷懒', stream_id=None)
    batch = _insert_messages(db, STREAM_ID, [('user', '下午摸鱼被抓包')])
    outcome = asyncio.run(mine_batch(
        db, None, stream_id=STREAM_ID, batch=batch,
        bot_name=BOT_NAME, bot_names=BOT_NAMES,
        temperature=0.1, max_tokens=64,
    ))
    assert outcome.substring_hits == 1
    assert _row(db, '摸鱼')['sightings'] == 1


# ---------------------------------------------------------------- 阶梯挑选


def _seed_ladder(db, sightings: int, inferred_at: int) -> None:
    _seed_jargon(
        db, f'词{sightings}_{inferred_at}', sightings=sightings,
        inferred_at=inferred_at, status='pending')


def test_m4_ladder_selection(db) -> None:
    """4/8/25/100 各档判一次；跨档跳跃合并成一次；100 锁定。"""

    _seed_stream(db)
    # (sightings, inferred_at) → 是否应被选中
    cases = [
        (3, 0, False),    # 未到第一档
        (4, 0, True),     # 恰好第一档
        (5, 4, False),    # 在 4 档判过，未到 8
        (8, 4, True),     # 到 8 档
        (30, 8, True),    # 跨过 25 档（8 档判过）
        (50, 30, False),  # 25 档判过，未到 100
        (100, 25, True),  # 到锁定档
        (150, 100, False),  # 已锁定
        (99, 0, True),    # 从未判过且早已越过 4
    ]
    for sightings, inferred_at, _ in cases:
        _seed_ladder(db, sightings, inferred_at)
    selected = {
        str(row['term']) for row in select_inference_targets(db, limit=100)}
    for sightings, inferred_at, expected in cases:
        assert (f'词{sightings}_{inferred_at}' in selected) is expected, (
            f'sightings={sightings} inferred={inferred_at}')


def test_m4_selection_prefers_most_evidence(db) -> None:
    """证据多的排在前——存量高频误报最先被重判。"""

    _seed_stream(db)
    _seed_ladder(db, 20, 0)
    _seed_ladder(db, 6, 0)
    _seed_ladder(db, 12, 0)
    terms = [str(row['term']) for row in select_inference_targets(db, limit=3)]
    assert terms == ['词20_0', '词12_0', '词6_0']


# ---------------------------------------------------------------- 三步推断


def _evidence_batch(db) -> List[StoredMessage]:
    return _insert_messages(db, STREAM_ID, [
        ('user', '这波真是典中典'),
        ('assistant', '典中典是什么意思呀'),
        ('user', '就是烂活重复出现的意思'),
    ])


def _target_row(db, term: str) -> sqlite3.Row:
    return db.execute(
        'SELECT id, term, meaning, stream_id, status, sightings, evidence_ids,'
        ' inferred_at_sightings FROM jargon WHERE term = ?',
        (term,),
    ).fetchone()


def test_m5_infer_confirmed_when_meanings_differ(db) -> None:
    """③ 判「不同」→ confirmed，释义取带上下文那份，参考释义进了①。"""

    _seed_stream(db)
    batch = _evidence_batch(db)
    _seed_jargon(
        db, '典', meaning='旧释义', sightings=4, inferred_at=0,
        evidence_ids=json.dumps([batch[0].message_id]))
    provider = _ScriptedProvider([
        json.dumps({'meaning': '群里用「典」嘲讽烂活反复出现'}),
        json.dumps({'meaning': '典籍、经典著作'}),
        json.dumps({'same': False}),
    ])
    result = asyncio.run(infer_term(
        db, provider, _target_row(db, '典'),
        bot_name=BOT_NAME, temperature=0.1, max_tokens=64))
    assert result == 'confirmed'
    row = _row(db, '典')
    assert row['status'] == 'confirmed'
    assert row['meaning'] == '群里用「典」嘲讽烂活反复出现'
    assert row['inferred_at_sightings'] == 4
    # ①的提示词：参考释义带进去了，她的发言带「不采信」标记。
    first_prompt = provider.requests[0][0]['content']
    assert '旧释义' in first_prompt
    assert '不采信' in first_prompt


def test_m5_infer_normal_word_keeps_old_meaning(db) -> None:
    """③ 判「相同」→ 普通词：pending、旧释义保留、计数推进。"""

    _seed_stream(db)
    batch = _evidence_batch(db)
    _seed_jargon(
        db, '码', meaning='旧释义：代码', status='confirmed',
        sightings=8, inferred_at=4,
        evidence_ids=json.dumps([batch[0].message_id]))
    provider = _ScriptedProvider([
        json.dumps({'meaning': '指写代码、敲代码'}),
        json.dumps({'meaning': '代码的意思'}),
        json.dumps({'same': True}),
    ])
    result = asyncio.run(infer_term(
        db, provider, _target_row(db, '码'),
        bot_name=BOT_NAME, temperature=0.1, max_tokens=64))
    assert result == 'normal_word'
    row = _row(db, '码')
    assert row['status'] == 'pending'
    assert row['meaning'] == '旧释义：代码'
    assert row['inferred_at_sightings'] == 8


def test_m5_infer_insufficient_short_circuits(db) -> None:
    """① 答信息不足：不做②③，但推进计数防反复重试。"""

    _seed_stream(db)
    batch = _evidence_batch(db)
    _seed_jargon(
        db, '糊', meaning='', status='pending', sightings=4, inferred_at=0,
        evidence_ids=json.dumps([batch[0].message_id]))
    provider = _ScriptedProvider([
        json.dumps({'insufficient': True}),
    ])
    result = asyncio.run(infer_term(
        db, provider, _target_row(db, '糊'),
        bot_name=BOT_NAME, temperature=0.1, max_tokens=64))
    assert result == 'insufficient'
    assert len(provider.requests) == 1
    row = _row(db, '糊')
    assert row['inferred_at_sightings'] == 4
    assert row['status'] == 'pending'


def test_m5_infer_parse_failure_writes_nothing(db) -> None:
    """任一步解析失败不写库：半截结果不落成「已判定」。"""

    _seed_stream(db)
    batch = _evidence_batch(db)
    _seed_jargon(
        db, '绷', meaning='', status='pending', sightings=4, inferred_at=0,
        evidence_ids=json.dumps([batch[0].message_id]))
    provider = _ScriptedProvider([
        json.dumps({'meaning': '群里表示绷不住了'}),
        json.dumps({'meaning': '绷带'}),
        '同样的含义',  # ③ 不是 JSON
    ])
    result = asyncio.run(infer_term(
        db, provider, _target_row(db, '绷'),
        bot_name=BOT_NAME, temperature=0.1, max_tokens=64))
    assert result == 'parse_failed'
    row = _row(db, '绷')
    assert row['inferred_at_sightings'] == 0
    assert row['status'] == 'pending'


def test_m5_infer_all_candidates_format_failure_is_parse_failure(db) -> None:
    """候选全部格式失败也应被进程内拉黑，避免每轮重复烧模型请求。"""

    _seed_stream(db)
    batch = _evidence_batch(db)
    _seed_jargon(
        db, '绷', meaning='', status='pending', sightings=4, inferred_at=0,
        evidence_ids=json.dumps([batch[0].message_id]))

    result = asyncio.run(infer_term(
        db, _FormatFailingProvider(), _target_row(db, '绷'),
        bot_name=BOT_NAME, temperature=0.1, max_tokens=64))

    assert result == 'parse_failed'
    row = _row(db, '绷')
    assert row['inferred_at_sightings'] == 0
    assert row['status'] == 'pending'


def test_m5_events_carry_payload(db) -> None:
    """jargon_mined / jargon_inferred 事件的载荷字段齐。"""

    async def scenario() -> tuple[dict, dict]:
        _seed_stream(db)
        subscriber = observe_events.broadcaster.subscribe(
            exclude_kinds=frozenset({'llm_chunk'}),
        )
        batch = _insert_messages(db, STREAM_ID, [
            ('user', '这操作太迷惑了，完全看不懂他在想什么'),
            ('user', '迷惑行为大赏又添一员，截图都发群里了'),
            ('user', '闲聊两句别的：今天整体节奏还不错，上午把该看的材料都看完了，下午准备把剩下的一点收尾工作做完，晚上打算早点休息，明天还有明天的事情，不用都赶在今天这一时半会儿里，慢慢来就行，不着急这一两天。'),
        ])
        provider = _ScriptedProvider([
            json.dumps({'candidates': [{'term': '迷惑行为', 'line': 1}]}),
            json.dumps({'meaning': '让人看不懂的行为'}),
            json.dumps({'meaning': '令人困惑的举动'}),
            json.dumps({'same': True}),
        ])
        await mine_batch(
            db, provider, stream_id=STREAM_ID, batch=batch,
            bot_name=BOT_NAME, bot_names=BOT_NAMES,
            temperature=0.1, max_tokens=64)
        await infer_term(
            db, provider, _target_row(db, '迷惑行为'),
            bot_name=BOT_NAME, temperature=0.1, max_tokens=64)
        await asyncio.sleep(0.05)
        mined: dict = {}
        inferred: dict = {}
        while not subscriber.queue.empty():
            entry = subscriber.queue.get_nowait()
            if entry.get('kind') == 'jargon_mined':
                mined = entry
            elif entry.get('kind') == 'jargon_inferred':
                inferred = entry
        return mined, inferred

    mined, inferred = asyncio.run(scenario())
    assert mined['streamId'] == STREAM_ID
    assert mined['candidates'] == 1 and mined['added'] == 1
    assert mined['userMessages'] == 3
    assert inferred['term'] == '迷惑行为'
    assert inferred['result'] == 'normal_word'
    assert inferred['sightings'] == 1


# ---------------------------------------------------------------- 游标与开关


def test_m6_cursor_roundtrip(db) -> None:
    store = MemoryStore(db)
    assert read_cursor(store, STREAM_ID) == 0
    advance_cursor(store, STREAM_ID, 42)
    assert read_cursor(store, STREAM_ID) == 42
    assert read_cursor(store, STREAM_ID + 1) == 0


def test_m6_learn_switch_defaults_on(db) -> None:
    _seed_stream(db)
    assert jargon_learn_enabled(db, STREAM_ID) is True
    db.execute(
        "INSERT INTO meta (key, value) VALUES ('jargon:learn:7', '0')")
    db.commit()
    assert jargon_learn_enabled(db, STREAM_ID) is False
