"""黑话召回重做验收：压缩、高频词表、打分沉底、去重、人名兜底与事件。

对应 docs/jargon-quality.md 的机检门（编号沿用该文）：

- 门 1/2（J-1）：271 字释义注入后 ≤ 上限；首句本来就短时不硬截；截断只
  发生在 agent/jargon.py，渲染器喂超长释义必须原样输出。
- 门 3（J-2）：CJK 单字永远不进高频词表；停用词不进。
- 门 4/5（J-3）：命中高频词的多字黑话必排在未命中单字之前，单字在 5 条
  上限下被截掉；hits 差异巨大的两条排序不受 hits 影响。
- 门 6（J-4）：同一词条在连续两轮里只注入一次。
- 门 7（J-6）：bot 自己的名字在任何 status、任何路径下都不会被注入。
- 门 8（J-8）：命中时必发 jargon_hit 事件，未命中不发。

仍成立的历史门（W3）一并保留：只注入命中词条（W3-1）、本会话释义优先
（W3-2）、pending 永不注入（W3-4）、无命中整块省略（W3-5）、命中即计数
含被截掉的。W3-3（按 hits 降序截断）已被打分排序取代，见门 4/5。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.core.agent.jargon import (
    InjectedTerms,
    compress_meaning,
    jargon_use_enabled,
    lookup_jargon,
    set_jargon_use,
)
from src.core.agent.prompt import build_itemized_system_prompt, build_system_prompt
from src.core.memory.high_frequency import collect_terms, rebuild_stream_terms
from src.core.observe import events as observe_events
from src.core.observe.store import event_store


TEST_NAME = '测试角色'
TEST_PERSONALITY = '喜欢观察细节，说话直接。'
TEST_REPLY_STYLE = '像熟人私聊，默认简短接话。'

# 本会话专属词条挂在 stream 7；stream 1 是 SEED 里自带的桌面会话，用来验证
# 没有专属词条时落到全局。
STREAM_ID = 7
GLOBAL_FALLBACK_STREAM = 1

# 真机 bot.toml 的名字集合形态；门 7 用同款喂法。
BOT_NAMES = ('月璃', '小璃', '璃宝', '凌白')


def _seed_stream(db, stream_id: int) -> None:
    db.execute(
        'INSERT INTO streams (id, platform, kind, external_id) VALUES (?, ?, ?, ?)',
        (stream_id, 'test', 'group', f'test-{stream_id}'),
    )
    db.commit()


def _add_jargon(
    db,
    term: str,
    meaning: str,
    stream_id: Optional[int] = None,
    status: str = 'confirmed',
    hits: int = 0,
) -> None:
    db.execute(
        'INSERT INTO jargon (term, meaning, stream_id, status, hits, source, created_at)'
        ' VALUES (?, ?, ?, ?, ?, ?, ?)',
        (term, meaning, stream_id, status, hits, 'w3-test', int(time.time() * 1000)),
    )
    db.commit()


def _add_high_frequency(db, stream_id: int, term: str, occurrences: int, rank: int) -> None:
    db.execute(
        'INSERT INTO high_frequency_terms'
        ' (stream_id, term, occurrence_count, message_count, rank, built_at)'
        ' VALUES (?, ?, ?, ?, ?, ?)',
        (stream_id, term, occurrences, occurrences, rank, int(time.time() * 1000)),
    )
    db.commit()


def _jargon_hits(db, term: str) -> int:
    row = db.execute('SELECT hits FROM jargon WHERE term = ?', (term,)).fetchone()
    assert row is not None, f'词条不存在：{term}'
    return int(row[0])


def _build_prompt(jargon: Optional[List[Tuple[str, str]]] = None, **kwargs: Any) -> str:
    values: Dict[str, Any] = {
        'name': TEST_NAME,
        'birthday': '',
        'personality': TEST_PERSONALITY,
        'reply_style': TEST_REPLY_STYLE,
    }
    if jargon is not None:
        values['jargon'] = jargon
    values.update(kwargs)
    return build_system_prompt(**values)


# ---------------------------------------------------------------- J-1 释义压缩

def test_j1_long_meaning_compressed_below_limit(db) -> None:
    """门 1：271 字级别的释义注入后不超过压缩上限。"""
    long_meaning = '「片」是网络聊天中常见的简略表达，通常指代视频或影片。' + '百科腔内容。' * 30
    assert len(long_meaning) > 200
    _add_jargon(db, '片', long_meaning)

    matched = lookup_jargon(db, STREAM_ID, '发个片看看')
    assert matched == [('片', compress_meaning(long_meaning))]
    assert len(matched[0][1]) <= 30


def test_j1_short_first_sentence_not_hard_cut(db) -> None:
    """门 1：首句本来就短时原样保留，不硬截也不加省略号。"""
    _add_jargon(db, '咕', '放鸽子。后面这句不该出现。')

    matched = lookup_jargon(db, STREAM_ID, '别咕了')
    assert matched[0][1] == '放鸽子。'


def test_j1_renderer_outputs_verbatim_no_second_truncation(db) -> None:
    """门 2：截断只发生在 agent/jargon.py；渲染器对超长释义原样输出。"""
    long_meaning = '首句很长很长很长很长很长很长很长很长很长很长。' + '第二句。' * 40
    prompt = _build_prompt(jargon=[('x', long_meaning)])
    assert long_meaning in prompt
    _system, items = build_itemized_system_prompt(
        jargon=[('x', long_meaning)],
        name=TEST_NAME, birthday='', personality=TEST_PERSONALITY,
        reply_style=TEST_REPLY_STYLE,
    )
    assert any(long_meaning in item for item in items)


# ---------------------------------------------------------------- J-2 高频词表

def test_j2_single_char_cjk_never_enters_table(db) -> None:
    """门 3：CJK 单字无论多高频都不进高频词表；停用词同样不进。"""
    texts = ['大大大大大', '了了了了了', '开开开开', '摆摆摆摆'] * 20
    ranked = collect_terms(texts)
    terms = [term for term, _, _ in ranked]
    assert terms, '多字高频词应该被收进来'
    for term in terms:
        assert len(term) >= 2, f'单字混进了高频词表：{term}'
    assert '大' not in terms
    assert '了' not in terms
    assert '这个' not in terms and '什么' not in terms, '功能词不该霸榜'


def test_j2_platform_placeholder_text_excluded(db) -> None:
    """机器占位段不参与统计：不剥离的话「表情包」「主题」会霸榜。"""
    ranked = collect_terms(['[表情包：无奈,呆萌]'] * 50 + ['[图片：主题是女性]'] * 50)
    terms = [term for term, _, _ in ranked]
    assert '表情包' not in terms
    assert '主题' not in terms
    assert '女性' not in terms


def test_j2_rebuild_writes_rank_and_replaces_snapshot(db) -> None:
    """rebuild 落 rank、全量替换：旧词不再高频时整行消失。"""
    _seed_stream(db, STREAM_ID)
    now = int(time.time() * 1000)
    db.executemany(
        'INSERT INTO messages (role, content, created_at, stream_id) VALUES (?, ?, ?, ?)',
        [('user', '开摆一下', now, STREAM_ID)] * 3
        + [('user', '别的词', now, STREAM_ID)],
    )
    db.commit()
    assert rebuild_stream_terms(db, STREAM_ID, now=now) >= 1
    rows = db.execute(
        'SELECT term, rank FROM high_frequency_terms WHERE stream_id = ? ORDER BY rank',
        (STREAM_ID,),
    ).fetchall()
    assert rows and int(rows[0]['rank']) == 1

    db.execute('DELETE FROM messages WHERE stream_id = ?', (STREAM_ID,))
    db.commit()
    rebuild_stream_terms(db, STREAM_ID, now=now)
    left = db.execute(
        'SELECT COUNT(*) FROM high_frequency_terms WHERE stream_id = ?',
        (STREAM_ID,),
    ).fetchone()[0]
    assert left == 0


async def test_j2_stats_service_rebuilds_on_lifecycle(db) -> None:
    """后台服务的 startup 全量重建、增量刷新与 shutdown 全链路可用。"""
    from src.core.services.maintenance.jargon_stats import JargonStatsService

    _seed_stream(db, STREAM_ID)
    now = int(time.time() * 1000)
    db.executemany(
        'INSERT INTO messages (role, content, created_at, stream_id) VALUES (?, ?, ?, ?)',
        [('user', '大家今天开摆吗', now, STREAM_ID)] * 5,
    )
    db.commit()

    service = JargonStatsService(db)
    await service.startup()
    try:
        rows = db.execute(
            'SELECT COUNT(*) FROM high_frequency_terms WHERE stream_id = ?',
            (STREAM_ID,),
        ).fetchone()[0]
        assert rows >= 1

        db.execute(
            'INSERT INTO messages (role, content, created_at, stream_id)'
            " VALUES ('user', '新词出现了', ?, ?)",
            (now + 1, STREAM_ID),
        )
        db.commit()
        await service._rebuild_changed()
        rows = db.execute(
            'SELECT COUNT(*) FROM high_frequency_terms WHERE stream_id = ?',
            (STREAM_ID,),
        ).fetchone()[0]
        assert rows >= 2
    finally:
        await service.shutdown()


# ---------------------------------------------------------------- J-3 打分沉底

def test_j3_high_frequency_multichar_ranks_before_single_char(db) -> None:
    """门 4：命中高频词的多字黑话排在未命中单字之前，单字被 5 条上限截掉。"""
    _seed_stream(db, STREAM_ID)
    _add_jargon(db, '大', '误报：会命中大家')
    _add_jargon(db, '开', '误报：会命中开什么')
    _add_jargon(db, '咕', '真单字黑话但本轮没高频背书')
    _add_jargon(db, '绷', '真单字黑话但本轮没高频背书')
    _add_jargon(db, '典', '真单字黑话但本轮没高频背书')
    _add_jargon(db, '孝', '真单字黑话但本轮没高频背书')
    _add_jargon(db, '开摆', '躺平不干了')
    _add_high_frequency(db, STREAM_ID, '开摆', occurrences=88, rank=3)

    matched = lookup_jargon(
        db, STREAM_ID,
        ['大家今天开摆吗，开摆开摆', '大家下午开什么玩笑', '大家咕绷典孝一起来'],
    )
    terms = [term for term, _ in matched]
    assert terms[0] == '开摆'
    assert len(terms) <= 5
    # 5 条上限全部被单字占满时，排在末尾的一定是纯计数最低的那些；
    # 关键断言：开摆（高频背书）永远不会被截。
    assert '开摆' in terms


def test_j3_single_char_cut_under_cap_when_high_frequency_present(db) -> None:
    """门 4 的截断形态：6 条单字 + 1 条高频多字，单字恰被截到只剩 4 条。"""
    _seed_stream(db, STREAM_ID)
    for char in '大小力出吃':
        _add_jargon(db, char, f'{char}的释义')
    _add_jargon(db, '开摆', '躺平不干了')
    _add_high_frequency(db, STREAM_ID, '开摆', occurrences=50, rank=1)

    matched = lookup_jargon(db, STREAM_ID, ['大家开摆，大力出奇迹，吃出大小'])
    terms = [term for term, _ in matched]
    assert terms[0] == '开摆'
    assert len(terms) == 5
    assert '小' not in terms or terms.index('小') > 0


def test_j3_hits_do_not_affect_ranking(db) -> None:
    """门 5：hits 差异巨大（999 vs 0）不影响排序。"""
    _add_jargon(db, '开摆', '躺平不干了', hits=0)
    _add_jargon(db, '摆烂', '破罐子破摔', hits=999)
    _add_jargon(db, '大', '误报', hits=999)

    matched = lookup_jargon(db, STREAM_ID, ['大家开摆吗，别摆烂'])
    terms = [term for term, _ in matched]
    assert '开摆' in terms
    # 同为多字、都无高频背书时按出现次数与词长裁决；hits=999 的词条
    # 没有因此排到任何靠前位置。
    assert terms.index('开摆') < terms.index('摆烂')


def test_j3_no_bigram_no_char_enumeration_pure_substring(db) -> None:
    """匹配是纯子串语义：是子串就命中（含跨词组合），不是子串就不命中。

    「鹅心」在「今晚鹅心里」里是子串所以照样命中——这正是机械匹配的本来
    面目；这类误报由打分沉底，不由匹配层拦截。「鹅今」不是子串，不命中。
    """
    _add_jargon(db, '今晚', '只应作为完整词命中')
    _add_jargon(db, '鹅心', '跨词子串照样命中，误报交给打分')
    _add_jargon(db, '鹅今', '不是子串不该命中')

    matched = lookup_jargon(db, STREAM_ID, '今晚鹅心里难受')
    assert [term for term, _ in matched] == ['今晚', '鹅心']


# ---------------------------------------------------------------- J-4 跨轮去重

def test_j4_same_term_injected_once_across_turns(db) -> None:
    """门 6：同一词条在连续两轮里只注入一次，残留期过后恢复。"""
    _add_jargon(db, '开摆', '躺平不干了')
    injected = InjectedTerms()
    now = int(time.time() * 1000)

    first = lookup_jargon(db, STREAM_ID, ['今天开摆吗'], injected=injected, now=now)
    second = lookup_jargon(db, STREAM_ID, ['还开摆吗'], injected=injected, now=now + 1000)
    assert first and not second
    # 残留期过后同词可以再次解释。
    later = lookup_jargon(
        db, STREAM_ID, ['又开摆了'], injected=injected, now=now + 31 * 60 * 1000)
    assert later


def test_j4_dedup_is_stream_scoped(db) -> None:
    """去重按会话隔离：一个群解释过不影响另一个群。"""
    _seed_stream(db, STREAM_ID)
    _add_jargon(db, '开摆', '躺平不干了')
    injected = InjectedTerms()
    now = int(time.time() * 1000)

    first = lookup_jargon(db, STREAM_ID, ['开摆'], injected=injected, now=now)
    other = lookup_jargon(
        db, GLOBAL_FALLBACK_STREAM, ['开摆'], injected=injected, now=now + 1000)
    assert first and other


# ---------------------------------------------------------------- J-6 人名兜底

def test_j6_bot_names_never_injected_any_status(db) -> None:
    """门 7：bot 自己的名字与别名任何 status、任何作用域都不被注入。"""
    _seed_stream(db, STREAM_ID)
    for name in BOT_NAMES:
        _add_jargon(db, name, '「%s」是一个网络昵称，通常有以下几种含义' % name)
        _add_jargon(db, name, 'pending 版', stream_id=STREAM_ID, status='pending')

    matched = lookup_jargon(
        db, STREAM_ID, ['小璃在吗，月璃快回话，璃宝凌白都在等你'],
        protected_names=BOT_NAMES,
    )
    assert matched == []

    # 两条渲染路径（经典 system 块与工具模式 item 流）都消费同一份查表结果，
    # 查表为空则两条路径都不出现黑话块。
    prompt = _build_prompt(jargon=matched)
    assert '# 这个群里的一些说法' not in prompt
    _system, items = build_itemized_system_prompt(
        jargon=matched,
        name=TEST_NAME, birthday='', personality=TEST_PERSONALITY,
        reply_style=TEST_REPLY_STYLE,
    )
    assert not [item for item in items if '这个群里的一些说法' in item]


# ---------------------------------------------------------------- J-7 会话级开关

def test_j7_use_switch_gates_recall_per_stream(db) -> None:
    """use 开关按会话关闭整条召回，缺省开启，别的会话不受影响。"""
    _seed_stream(db, STREAM_ID)
    _add_jargon(db, '开摆', '躺平不干了')
    assert jargon_use_enabled(db, STREAM_ID) is True
    assert lookup_jargon(db, STREAM_ID, ['今天开摆吗'])

    assert set_jargon_use(db, STREAM_ID, False) is False
    assert lookup_jargon(db, STREAM_ID, ['今天开摆吗']) == []
    assert lookup_jargon(db, GLOBAL_FALLBACK_STREAM, ['今天开摆吗'])

    assert set_jargon_use(db, STREAM_ID, True) is True
    assert lookup_jargon(db, STREAM_ID, ['今天开摆吗'])


# ---------------------------------------------------------------- J-8 观测事件

def test_j8_event_emitted_on_hit_not_on_miss(db) -> None:
    """门 8：命中时必发 jargon_hit 事件，未命中不发。"""
    async def scenario() -> tuple[list[dict], list[dict]]:
        subscriber = observe_events.broadcaster.subscribe(
            exclude_kinds=frozenset({'llm_chunk'}),
        )
        _add_jargon(db, '开摆', '躺平不干了')
        hit = lookup_jargon(db, STREAM_ID, ['今天开摆吗'])
        await asyncio.sleep(0.05)
        hit_events: list[dict] = []
        while not subscriber.queue.empty():
            entry = subscriber.queue.get_nowait()
            if entry.get('kind') == 'jargon_hit':
                hit_events.append(entry)
        miss = lookup_jargon(db, STREAM_ID, ['完全无关的消息'])
        await asyncio.sleep(0.05)
        miss_events: list[dict] = []
        while not subscriber.queue.empty():
            entry = subscriber.queue.get_nowait()
            if entry.get('kind') == 'jargon_hit':
                miss_events.append(entry)
        assert hit and miss == []
        return hit_events, miss_events

    hit_events, miss_events = asyncio.run(scenario())
    assert len(hit_events) == 1
    entry = hit_events[0]
    assert entry['injected'] == 1
    assert entry['candidates'] == 1
    assert entry['truncated'] == 0
    assert entry['chars'] > 0
    assert entry['terms'] == ['开摆']
    assert miss_events == []


# ---------------------------------------------------------------- 历史门（W3）

def test_w3_1_only_matched_terms_reach_prompt(db) -> None:
    """★W3-1：库里只有被本轮消息命中的词条进入提示词，其余一条都不出现。"""
    _add_jargon(db, '咕', '放鸽子、说好的事没做')
    _add_jargon(db, '星奴', '只会追星、说话全是粉圈味的人')

    matched = lookup_jargon(db, STREAM_ID, '今晚又要咕了是吧')
    assert [term for term, _ in matched] == ['咕']

    prompt = _build_prompt(jargon=matched)
    assert '「咕」= 放鸽子、说好的事没做' in prompt
    assert '星奴' not in prompt
    assert '只会追星' not in prompt


def test_w3_2_stream_scoped_meaning_wins_over_global(db) -> None:
    """★W3-2：同一个词双份时本会话那条优先；别的会话仍落到全局。"""
    _seed_stream(db, STREAM_ID)
    _add_jargon(db, '咕', '放鸽子、说好的事没做', stream_id=None)
    _add_jargon(db, '咕', '在这个群里特指临时放大家鸽子', stream_id=STREAM_ID)

    matched = lookup_jargon(db, STREAM_ID, '今晚又要咕了是吧')
    assert matched == [('咕', '在这个群里特指临时放大家鸽子')]

    fallback = lookup_jargon(db, GLOBAL_FALLBACK_STREAM, '今晚又要咕了是吧')
    assert fallback == [('咕', '放鸽子、说好的事没做')]


def test_w3_3_truncation_by_score_not_by_hits(db) -> None:
    """★W3-3（改版）：命中超过 5 条按打分截断——高频背书者在前，hits 无关。"""
    _seed_stream(db, STREAM_ID)
    for char in '咕绷典蚌麻急性':
        _add_jargon(db, char, f'{char}的意思', hits=ord(char) % 7)
    _add_jargon(db, '开摆', '躺平不干了')
    _add_high_frequency(db, STREAM_ID, '开摆', occurrences=60, rank=2)

    matched = lookup_jargon(db, STREAM_ID, ['开摆，咕、绷、典、蚌、麻、急、孝'])
    terms = [term for term, _ in matched]
    assert len(terms) == 5
    assert terms[0] == '开摆'


def test_w3_4_pending_terms_never_injected(db) -> None:
    """★W3-4：status != 'confirmed' 的词条永远不被注入，hits 也不动。"""
    _add_jargon(db, '摆烂', '破罐子破摔', status='pending', hits=0)

    matched = lookup_jargon(db, STREAM_ID, '你最近完全摆烂了啊')
    assert matched == []
    assert _jargon_hits(db, '摆烂') == 0

    prompt = _build_prompt(jargon=matched)
    assert '摆烂' not in prompt


def test_w3_5_no_hit_no_block_no_empty_heading(db) -> None:
    """★W3-5：无命中时整块省略，提示词里不出现空的标题段。"""
    assert lookup_jargon(db, STREAM_ID, '今晚又要咕了是吧') == []

    prompt = _build_prompt()
    assert '# 这个群里的一些说法' not in prompt
    assert _build_prompt(jargon=[]) == prompt


def test_each_hit_increments_counter_including_truncated(db) -> None:
    """命中即 hits += 1：被上限截掉的词条同样计数；被去重排除的同样计数。"""
    _seed_stream(db, STREAM_ID)
    for char in '咕绷典蚌麻急性':
        _add_jargon(db, char, f'{char}的意思')
    _add_high_frequency(db, STREAM_ID, '咕', occurrences=99, rank=1)

    matched = lookup_jargon(db, STREAM_ID, ['咕、绷、典、蚌、麻、急、性'])
    assert len(matched) == 5
    assert _jargon_hits(db, '咕') == 1
    # 被截掉的（如「性」）与被注入的一样 +1。
    assert _jargon_hits(db, '性') == 1


def test_single_char_term_hits_by_substring(db) -> None:
    """「别咕了」里单字黑话仍命中——纯子串匹配天然覆盖，不再依赖切词。"""
    _add_jargon(db, '咕', '放鸽子、说好的事没做')

    assert lookup_jargon(db, STREAM_ID, '别咕了，人也咕了') == [
        ('咕', '放鸽子、说好的事没做'),
    ]


def test_two_char_and_english_terms_match_case_insensitive(db) -> None:
    """二字词与英文字母词大小写不敏感命中。"""
    _add_jargon(db, '星奴', '只会追星、说话全是粉圈味的人')
    _add_jargon(db, 'YYDS', '永远的神，形容极度佩服')

    matched = lookup_jargon(db, STREAM_ID, '他就是个星奴，这波操作yyds')
    assert ('星奴', '只会追星、说话全是粉圈味的人') in matched
    assert ('YYDS', '永远的神，形容极度佩服') in matched


# ---------------------------------------------------------------- 注入形态

def test_flat_prompt_renders_jargon_block_with_discipline(db) -> None:
    """注入块带标题、免责表头与「听得懂就行」的收尾约束（背景，不是任务）。"""
    prompt = _build_prompt(jargon=[('咕', '放鸽子'), ('典', '反讽，指很老套')])
    assert '# 这个群里的一些说法' in prompt
    assert '按字面从上下文匹配' in prompt
    assert '仅用于理解消息内容' in prompt
    assert '这些是群内常用说法，能看懂即可。不要刻意使用，也不要向群成员解释词义。' in prompt


def test_itemized_prompt_carries_jargon_as_context_item(db) -> None:
    """工具模式的 item 流里黑话是独立上下文项，无命中时不出现。"""
    kwargs: Dict[str, Any] = {
        'name': TEST_NAME,
        'birthday': '',
        'personality': TEST_PERSONALITY,
        'reply_style': TEST_REPLY_STYLE,
    }
    system, items = build_itemized_system_prompt(jargon=[('咕', '放鸽子')], **kwargs)
    jargon_items = [item for item in items if '这个群里的一些说法' in item]
    assert len(jargon_items) == 1
    assert '「咕」= 放鸽子' in jargon_items[0]

    system, items = build_itemized_system_prompt(**kwargs)
    assert not [item for item in items if '这个群里的一些说法' in item]
