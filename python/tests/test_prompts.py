"""提示词结构与去人机感规则回归。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncIterator, Dict, List

import random

from yueli.agent.character import TONE_VARIANTS, pick_tone
from yueli.agent.expression import (
    detect_buckets, render_expression_habits, select_expression_habits,
)
from yueli.agent.prompt import build_proactive_prompt, build_system_prompt
from yueli.agent.summarize import summarize
from yueli.persona.state import PersonaState, describe_persona
from yueli.schedule.plan import build_plan_prompt
from yueli.services.vision import VisionService


class _SummaryProvider:
    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        **_kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        self.messages = messages
        yield {
            'text': '{"summary":"他跟我说项目终于跑通了，我也替他松了口气。",'
                    '"recall_cues":["以后再聊这个项目跑通的时候"]}',
        }


def test_main_prompt_separates_identity_behavior_and_reply_style() -> None:
    prompt = build_system_prompt(
        now=datetime(2032, 7, 15, 23, 40),
        schedule='',
    )

    assert '# 你是谁' in prompt
    assert '# 这一刻怎么接话' in prompt
    assert '# 说话的味道' in prompt
    assert '# 输出格式' in prompt
    assert '他只是随手分享' in prompt
    assert '把这里当成两个人的私聊' in prompt


def test_time_and_memory_are_context_not_mandatory_topics() -> None:
    prompt = build_system_prompt(
        now=datetime(2032, 7, 15, 12, 5),
        facts=['他不吃香菜'],
        episodes=['昨天聊到一个难修的报错，最后还没解决。'],
        activity='他在写代码。',
        schedule='',
    )

    assert '时间只是这段对话的背景' in prompt
    assert '用不上就放着' in prompt
    assert '不要为了证明记得而主动翻旧账' in prompt
    assert '不是监控报告' in prompt
    assert '深夜要劝他早点睡' not in prompt
    assert '饭点要问吃没吃' not in prompt


def test_output_protocol_uses_consistent_memory_tag() -> None:
    prompt = build_system_prompt(schedule='')

    assert '<memory type="类别">一句完整、客观的事实</memory>' in prompt
    assert '</system_reminder>' not in prompt
    assert '普通寒暄不用硬凑 mood 标签' in prompt


def test_proactive_prompt_avoids_monitoring_and_health_check_templates() -> None:
    prompt = build_proactive_prompt('基础人设', '他已经写了一阵代码。')

    assert '这不是系统通知，也不是关怀任务' in prompt
    assert '不要把情境原样播报给他' in prompt
    assert '不要用「我注意到」「检测到」「你似乎正在」开头' in prompt
    assert '不要固定落到休息、喝水或早点睡' in prompt


async def test_summary_agent_uses_memory_voice_instead_of_meeting_minutes() -> None:
    provider = _SummaryProvider()
    episode = await summarize(provider, [
        {'role': 'user', 'content': '折腾了一晚上，那个项目终于跑通了，原来只是配置文件里少写了一个字段。'},
        {'role': 'assistant', 'content': '<say emotion="happy">真的？难怪你卡了这么久……这下我也跟着松口气了。</say>'},
    ])

    assert episode is not None
    assert provider.messages[0]['role'] == 'system'
    assert '不是会议纪要' in provider.messages[0]['content']
    assert '我们围绕某话题进行了交流' in provider.messages[0]['content']
    assert provider.messages[1]['role'] == 'user'


def test_schedule_agent_builds_a_life_instead_of_a_duty_roster() -> None:
    prompt = build_plan_prompt(
        date='2032-07-15',
        weekday='周四',
        occasion='没有特别节日',
        persona='今天有点累。',
        yesterday_theme='把没看完的短篇读完。',
        yesterday_bedtime='23:40',
        yesterday_wake='08:10',
        yesterday_carry_over='还剩最后几页。',
        yesterday_avoided='整理旧图',
        density='最近偶尔聊聊。',
    )

    assert '不是等用户出现的值班表' in prompt
    assert '不要排成自律博主的打卡清单' in prompt
    assert '她有些事只是自己想做' in prompt
    assert '省略主语的日常片段' in prompt


def test_persona_state_does_not_force_repetitive_care() -> None:
    prompt = describe_persona(PersonaState(
        intimacy=95,
        tsundere=30,
        reliance=95,
        energy=10,
        updated_at=0,
    ))

    assert '别机械地索要保证' in prompt
    assert '不要自动催他睡觉' in prompt
    assert '反复确认他还在' not in prompt


def test_expression_buckets_follow_what_he_just_said() -> None:
    assert 'trouble' in detect_buckets('这破报错折腾一晚上了，烦死')
    assert 'joke' in detect_buckets('哈哈哈笑死我了')
    assert 'ask' in detect_buckets('这个为什么会这样？')
    assert 'short' in detect_buckets('嗯')
    # 认不出情境时当成随手分享，而不是空手注入。
    assert detect_buckets('') == ['share']


def test_expression_samples_are_concrete_lines_not_style_adjectives() -> None:
    rng = random.Random(7)
    samples = select_expression_habits('卡在这个 bug 上一晚上了，累', limit=4, rng=rng)

    assert 1 <= len(samples) <= 4
    block = render_expression_habits(samples)
    assert '只借语感，不要照抄' in block
    assert block.count('- 当「') == len(samples)


def test_main_prompt_carries_expression_samples_and_attention_drift() -> None:
    prompt = build_system_prompt(
        schedule='',
        expression_habits=render_expression_habits([('他在抱怨一件很具体的麻烦', '顺着一起骂一句')]),
        tone=TONE_VARIANTS[0],
    )

    assert '# 她平时的说法' in prompt
    assert '当「他在抱怨一件很具体的麻烦」时' in prompt
    assert '你的注意力不是均匀的' in prompt
    assert '一轮最多一次明显的拐弯' in prompt
    assert TONE_VARIANTS[0] in prompt
    # 表达样本要贴着输出格式，别被边界和协议隔开太远。
    assert prompt.index('# 她平时的说法') > prompt.index('# 说话的味道')
    assert prompt.index('# 她平时的说法') < prompt.index('# 输出格式')


def test_optional_blocks_stay_out_when_nothing_selected() -> None:
    prompt = build_system_prompt(schedule='')

    assert '# 她平时的说法' not in prompt
    assert not any(tone in prompt for tone in TONE_VARIANTS)


def test_tone_variation_is_occasional_not_every_turn() -> None:
    rng = random.Random(3)
    picked = [pick_tone(rng) for _ in range(200)]

    assert None in picked
    assert any(tone is not None for tone in picked)
    assert all(tone in TONE_VARIANTS for tone in picked if tone is not None)
    assert pick_tone(rng, probability=0) is None


def test_proactive_prompt_shows_how_to_open_not_only_what_to_avoid() -> None:
    prompt = build_proactive_prompt('基础人设', '他已经写了一阵代码。')

    assert '大概是这种起头方式' in prompt
    assert '也可以从你自己那边起头' in prompt


def test_summary_agent_keeps_a_specific_anchor_for_later_recall() -> None:
    from yueli.agent.summarize import _SYSTEM_PROMPT

    assert '具体锚点' in _SYSTEM_PROMPT
    assert '以后在什么情境下会需要想起这段' in _SYSTEM_PROMPT


def test_vision_agent_returns_observation_instead_of_assumptions() -> None:
    prompt = VisionService._build_vision_prompt('gameplay')

    assert '客观情境线索' in prompt
    assert '不要猜游戏名、剧情或玩家感受' in prompt
