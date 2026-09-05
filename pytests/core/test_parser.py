"""
ResponseParser 测试。逐条移植自 src/core/agent/parser.test.ts。
解析器是最吃紧的一域：半截标签、隐式 say、漏写 </say>、<think> 丢弃。
"""

from __future__ import annotations

import pytest
from src.core.agent.parser import (
    DecisionEvent, MoodEvent, ParseEvent, PromiseEvent, ResponseParser,
    SayEndEvent, SayEvent, TextEvent,
)


def parse_all(src: str) -> list[ParseEvent]:
    p = ResponseParser()
    return [*p.push(src), *p.flush()]


def parse_by_char(src: str) -> list[ParseEvent]:
    """逐字符喂 —— 模拟最恶劣的分包，任何跨 chunk 的标签断裂都会暴露。"""
    p = ResponseParser()
    out: list[ParseEvent] = []
    for ch in src:
        out.extend(p.push(ch))
    out.extend(p.flush())
    return out


def said(events: list[ParseEvent]) -> str:
    return ''.join(e.value for e in events if isinstance(e, TextEvent))


class TestResponseParser:
    def test_standard_say_with_emotion(self):
        src = '<say emotion="害羞" gesture="捂胸口">诶、诶？你怎么突然……</say>'
        for events in [parse_all(src), parse_by_char(src)]:
            first = events[0]
            assert isinstance(first, SayEvent)
            assert first.emotion == '害羞'
            assert first.gesture == '捂胸口'
            assert said(events) == '诶、诶？你怎么突然……'
            assert isinstance(events[-1], SayEndEvent)

    def test_char_by_char_same_semantics(self):
        src = '<say emotion="开心">今天也要一起加油哦！</say>\n<memory type="偏好">玩家喜欢深夜写代码</system_reminder>\n<mood favor="+2" energy="-1"/>'

        def semantics(events):
            return {
                'text': said(events),
                'rest': [e for e in events if not isinstance(e, TextEvent)],
            }

        assert semantics(parse_by_char(src)) == semantics(parse_all(src))

    def test_memory_tag_is_swallowed_not_parsed(self):
        """<memory> 已废止：既不产出事件，也绝不能漏进可见台词。

        事实抽取改由回合之后的后台任务承担（agent/fact_extract.py）。解析器保留
        对该标签的识别只为一件事——把它连同内容一起吞掉；若改成当未知标签处理，
        整段协议外壳会被 _emit_text 当成正文说出去。
        """
        events = parse_all('<memory type="偏好">玩家喜欢深夜写代码</system_reminder><mood favor="+2" energy="-1"/>')
        mood = next((e for e in events if isinstance(e, MoodEvent)), None)
        assert said(events) == ''
        assert mood is not None and mood.favor == 2.0 and mood.energy == -1.0

    def test_memory_tag_swallowed_across_chunks(self):
        """标签被网络分片切断时同样不能漏字，且不影响前后台词。"""
        parser = ResponseParser()
        events: list[ParseEvent] = []
        for chunk in ('<say>好呀</say><mem', 'ory type="喜好">她喜欢',
                      '吃辣</mem', 'ory>后面还有话'):
            events += parser.push(chunk)
        events += parser.flush()
        assert said(events) == '好呀后面还有话'

    def test_unclosed_memory_tail_is_dropped(self):
        """未闭合的废止标签，残留内容必须丢弃而不是当台词吐出去。"""
        events = parse_all('<say>嗯<memory>没闭合的记忆')
        assert said(events) == '嗯' 

    def test_plain_text_not_lost(self):
        events = parse_all('我今天有点累……')
        assert isinstance(events[0], SayEvent)
        assert said(events) == '我今天有点累……'
        assert isinstance(events[-1], SayEndEvent)

    def test_missing_close_tag_flushed(self):
        events = parse_all('<say emotion="开心">话说到一半就断了')
        assert said(events) == '话说到一半就断了'
        assert isinstance(events[-1], SayEndEvent)

    def test_think_tag_is_plain_text(self):
        events = parse_all('<think>用户看起来心情不好，我该安慰他。</think><say emotion="温柔">怎么了？</say>')
        assert said(events) == '<think>用户看起来心情不好，我该安慰他。</think>怎么了？'

    def test_think_tag_split_across_chunks_is_plain_text(self):
        src = '<think>盘算中……</think><say>好呀</say>'
        assert said(parse_by_char(src)) == '<think>盘算中……</think>好呀'

    def test_bare_lt_in_text(self):
        events = parse_all('<say>如果 a < b 那么……</say>')
        assert said(events) == '如果 a < b 那么……'
        assert isinstance(events[-1], SayEndEvent)

    def test_unknown_tag_not_leaked(self):
        events = parse_all('<say>前<foo bar="1">后</say>')
        text = said(events)
        assert '前' in text
        assert '后' in text
        assert isinstance(events[-1], SayEndEvent)

    def test_multiple_say_with_emotion(self):
        events = parse_all('<say emotion="开心">第一句</say><say emotion="害羞">第二句</say>')
        says = [e for e in events if isinstance(e, SayEvent)]
        assert len(says) == 2
        assert says[0].emotion == '开心'
        assert says[1].emotion == '害羞'
        assert len([e for e in events if isinstance(e, SayEndEvent)]) == 2
        assert said(events) == '第一句第二句'

    def test_whitespace_between_tags_no_empty_say(self):
        events = parse_all('<say>甲</say>\n\n  \n<mood favor="+1"/>\n<say>乙</say>')
        assert said(events) == '甲乙'

    def test_single_quote_and_unquoted_attrs(self):
        events = parse_all("<SAY Emotion='生气' gesture=比心>哼</SAY>")
        first = events[0]
        assert isinstance(first, SayEvent)
        assert first.emotion == '生气'
        assert first.gesture == '比心'
        assert said(events) == '哼'

    def test_mood_non_numeric_no_event(self):
        moods = [e for e in parse_all('<mood favor="很多"/>') if isinstance(e, MoodEvent)]
        assert len(moods) == 0

    def test_mood_inside_say_not_in_text(self):
        events = parse_all('<say emotion="开心">好耶<mood favor="+3"/>！</say>')
        assert said(events) == '好耶！'
        moods = [e for e in events if isinstance(e, MoodEvent)]
        assert len(moods) == 1 and moods[0].favor == 3.0

    def test_promise_and_mood_can_be_mixed_without_interference(self):
        events = parse_all(
            '<mood favor="+2"/><promise at="2026-08-08 20:00" what="一起打游戏"/>'
        )
        mood = next(event for event in events if isinstance(event, MoodEvent))
        promise = next(event for event in events if isinstance(event, PromiseEvent))
        assert mood.favor == 2
        assert promise.at > 0 and promise.what == '一起打游戏'

    @pytest.mark.parametrize('src', [
        '<promise what="一起打游戏"/>',
        '<promise at="周六晚上" what="一起打游戏"/>',
        '<promise at="2026-08-08 20:00" what=""/>',
    ])
    def test_malformed_promise_is_discarded_without_guessing(self, src: str):
        assert not [event for event in parse_all(src) if isinstance(event, PromiseEvent)]

    def test_streaming_text_incremental(self):
        p = ResponseParser()
        result = p.push('<say emotion="开心">')
        assert len(result) == 1 and isinstance(result[0], SayEvent) and result[0].emotion == '开心'
        assert p.push('你') == [TextEvent(value='你')]
        assert p.push('好') == [TextEvent(value='好')]

    def test_empty_input_no_events(self):
        assert parse_all('') == []

    def test_half_tag_no_garbage(self):
        p = ResponseParser()
        assert p.push('<sa') == []
        assert p.flush() == []

class TestDecisionTag:
    """动作头标签的解析：属性原文透传，语义校验留给 Agent。"""

    def test_decision_attributes_passed_through(self):
        src = (
            '<decision action="reply" targets="101,102" quote="101" '
            'reasons="direct_question,topic_continuation" length="brief"/>'
        )
        for events in [parse_all(src), parse_by_char(src)]:
            head = next(e for e in events if isinstance(e, DecisionEvent))
            assert head.action == "reply"
            assert head.targets == "101,102"
            assert head.quote == "101"
            assert head.reasons == "direct_question,topic_continuation"
            assert head.length == "brief"

    def test_decision_split_across_chunks(self):
        p = ResponseParser()
        assert p.push("<deci") == []
        assert p.push('sion action="silent" reasons="low_relevance"/>') == [
            DecisionEvent(action="silent", targets=None, quote=None,
                          reasons="low_relevance", length=None),
        ]

    def test_closing_decision_tag_ignored(self):
        events = parse_all("<decision action=\"reply\" targets=\"101\" reasons=\"direct_question\" length=\"brief\"></decision>")
        heads = [e for e in events if isinstance(e, DecisionEvent)]
        assert len(heads) == 1

    def test_decision_does_not_enter_text_state(self):
        src = (
            '<decision action="reply" targets="101" reasons="direct_question" '
            'length="brief"/><say>在的</say>'
        )
        events = parse_all(src)
        assert isinstance(events[0], DecisionEvent)
        assert said(events) == "在的"

