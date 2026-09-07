"""提示词结构与去人机感规则回归。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncIterator, Dict, List

import random

from src.core.agent.character import pick_tone
from src.core.agent.expression import ExpressionSample, render_expression_habits
from src.core.agent.fact_extract import Participant
from src.core.agent.prompt import build_proactive_prompt, build_system_prompt
from src.core.agent.summarize import summarize
from src.core.config.schema import Config
from src.core.persona.state import PersonaState, describe_persona
from src.core.schedule.plan import build_plan_prompt
from src.desktop.vision import VisionService


TEST_NAME = '测试角色'
TEST_PERSONALITY = '喜欢观察细节，说话直接。'
TEST_REPLY_STYLE = '像熟人私聊，默认简短接话。'
TEST_TONES = [
    '这一轮用很短的话接。',
    '这一轮可以顺手吐槽一句。',
]


def _build_prompt(**kwargs: Any) -> str:
    values: Dict[str, Any] = {
        'name': TEST_NAME,
        'birthday': '',
        'personality': TEST_PERSONALITY,
        'reply_style': TEST_REPLY_STYLE,
        'schedule': '',
    }
    values.update(kwargs)
    return build_system_prompt(**values)


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


def test_main_prompt_separates_configured_personality_and_fixed_rules() -> None:
    prompt = _build_prompt(
        now=datetime(2032, 7, 15, 23, 40),
    )

    assert '# 你是谁' in prompt
    assert '# 说话风格' in prompt
    assert '# 事实纪律' in prompt
    assert '# 输出格式' in prompt
    assert TEST_PERSONALITY in prompt
    assert TEST_REPLY_STYLE in prompt


def test_time_and_memory_are_context_not_mandatory_topics() -> None:
    prompt = _build_prompt(
        now=datetime(2032, 7, 15, 12, 5),
        facts=['他不吃香菜'],
        episodes=['昨天聊到一个难修的报错，最后还没解决。'],
        activity='他在写代码。',
    )

    assert '时间只是这段对话的背景' in prompt
    assert '不需要时不提' in prompt
    assert '不要为了证明记得而主动翻旧账' in prompt
    assert '不是监控报告' in prompt
    assert '深夜要劝他早点睡' not in prompt
    assert '饭点要问吃没吃' not in prompt


def test_output_protocol_no_longer_asks_for_memory_tag() -> None:
    """★W1-5：三份模板都不得再要求模型打 <memory> 标签。

    让正在说话的模型顺手打标签这个形态在真机上 192 次调用产出 0 条，事实抽取已经
    改由回合之后的独立后台任务承担。协议里留着这条规则就是第二条产出同一结果的
    路径——本项目明确反对，且它会持续占用提示词预算换取零产出。
    """
    prompt = _build_prompt()

    assert '<memory' not in prompt
    assert '值得长期记住的稳定事实' not in prompt
    assert '</system_reminder>' not in prompt
    # mood 与 promise 不在本次范围内，仍应完整保留。
    assert '普通寒暄不用硬凑 mood 标签' in prompt


def test_every_chat_template_is_free_of_memory_rule() -> None:
    """三份模板逐个查，避免只改了组装进 _build_prompt 的那一份。"""
    from src.core.prompts.registry import get_prompt

    for name in ('chat.protocol', 'chat.replyer', 'chat.action.protocol'):
        assert '<memory' not in get_prompt(name).text, name


def test_jargon_prompts_define_unambiguous_json_contracts() -> None:
    """黑话提示词必须阻止本次真机出现的裸双引号和互斥字段并存。"""

    from src.core.prompts.registry import get_prompt

    context = get_prompt('jargon.meaning.context').text
    bare = get_prompt('jargon.meaning.bare').text
    compare = get_prompt('jargon.compare').text
    mine = get_prompt('jargon.mine').text

    assert '必须二选一' in context
    assert '不能同时出现' in context
    assert '未转义的英文半角' in context
    assert '未转义的' in bare
    assert '必须且只能包含 `meaning`' in bare
    assert '必须且只能包含布尔类型的' in compare
    assert '必须且只能包含' in mine


def test_proactive_prompt_avoids_monitoring_and_health_check_templates() -> None:
    prompt = build_proactive_prompt('基础人设', '他已经写了一阵代码。')

    assert '这不是系统通知，也不是关怀任务' in prompt
    assert '不要把情境原样播报给对方' in prompt
    assert '不要用「我注意到」「检测到」「你似乎正在」开头' in prompt
    assert '不要固定落到休息、喝水或早点睡' in prompt


async def test_summary_agent_uses_memory_voice_instead_of_meeting_minutes() -> None:
    provider = _SummaryProvider()
    episode = await summarize(
        provider,
        [
            {
                'role': 'user',
                'content': '折腾了一晚上，那个项目终于跑通了，原来只是配置文件里少写了一个字段。',
                'sender_person_id': 1,
            },
            {'role': 'assistant', 'content': '<say emotion="happy">真的？难怪你卡了这么久……这下我也跟着松口气了。</say>'},
        ],
        temperature=Config().generation.summary.temperature,
        max_tokens=Config().generation.summary.token_limit,
        character_name=TEST_NAME,
        character_personality=TEST_PERSONALITY,
        participants=[Participant(external_id='10001', display_name='阿澈', person_id=1)],
    )

    assert episode is not None
    assert provider.messages[0]['role'] == 'system'
    assert '不是会议纪要' in provider.messages[0]['content']
    assert '我们围绕某话题进行了交流' in provider.messages[0]['content']
    assert provider.messages[1]['role'] == 'user'
    assert '阿澈：折腾了一晚上' in provider.messages[1]['content']


def test_summary_prompt_assigns_speech_to_present_speakers_only() -> None:
    """群聊归属规则：不合并到一个人，转发块里的名字不算在场发言者。"""
    from src.core.prompts.registry import get_prompt

    text = get_prompt('summary').text
    assert '不同人的发言不得合并或归到同一个人身上' in text
    assert '不算任何在场者的主张' in text
    assert '名字末尾带编号的是不同的人' in text


async def test_summary_render_keeps_group_speakers_separate() -> None:
    """群聊里两个不同 person 的发言必须渲染出两个不同前缀且都不是「对方」。"""
    provider = _SummaryProvider()
    await summarize(
        provider,
        [
            {
                'role': 'user',
                'content': '我将购入猫娘洗面奶，链接放这了，大半夜的别睡',
                'sender_person_id': 1,
            },
            {
                'role': 'user',
                'content': '[戳了戳 月璃] 那月璃可以给我一张你的腿照吗',
                'sender_person_id': 2,
            },
            {'role': 'assistant', 'content': '<say>想得美……大半夜的要什么腿照</say>'},
        ],
        temperature=Config().generation.summary.temperature,
        max_tokens=Config().generation.summary.token_limit,
        character_name=TEST_NAME,
        character_personality=TEST_PERSONALITY,
        participants=[
            Participant(external_id='10001', display_name='凌白', person_id=1),
            Participant(external_id='10002', display_name='龙之啸风', person_id=2),
        ],
    )

    user_content = provider.messages[1]['content']
    assert '在场的人：' in user_content
    assert '凌白：我将购入猫娘洗面奶' in user_content
    assert '龙之啸风：[戳了戳 月璃]' in user_content
    assert '对方：' not in user_content
    assert '某人' not in user_content
    assert '我：想得美' in user_content


async def test_summary_render_numbers_duplicate_display_names() -> None:
    """同名不同人靠末尾编号区分：渲染成相同前缀等于没有区分。"""
    provider = _SummaryProvider()
    await summarize(
        provider,
        [
            {
                'role': 'user',
                'content': '今晚一起上线打素材本吗，缺个奶',
                'sender_person_id': 1,
            },
            {
                'role': 'user',
                'content': '我这边周日才有空，周末要回家一趟',
                'sender_person_id': 2,
            },
        ],
        temperature=Config().generation.summary.temperature,
        max_tokens=Config().generation.summary.token_limit,
        character_name=TEST_NAME,
        character_personality=TEST_PERSONALITY,
        participants=[
            Participant(external_id='20001', display_name='小明', person_id=1),
            Participant(external_id='20002', display_name='小明', person_id=2),
        ],
    )

    user_content = provider.messages[1]['content']
    assert '小明1：今晚一起上线打素材本吗' in user_content
    assert '小明2：我这边周日才有空' in user_content


async def test_summary_render_falls_back_to_counterpart_without_participants() -> None:
    """解析不出名单的链路保持「对方」渲染且不出现「某人」。"""
    provider = _SummaryProvider()
    await summarize(
        provider,
        [
            {
                'role': 'user',
                'content': '折腾了一晚上，那个项目终于跑通了，原来只是配置文件里少写了一个字段。',
                'sender_person_id': 7,
            },
            {'role': 'assistant', 'content': '<say>真的？这下我也跟着松口气了。</say>'},
        ],
        temperature=Config().generation.summary.temperature,
        max_tokens=Config().generation.summary.token_limit,
        character_name=TEST_NAME,
        character_personality=TEST_PERSONALITY,
        participants=[],
    )

    user_content = provider.messages[1]['content']
    assert '对方：折腾了一晚上' in user_content
    assert '在场的人' not in user_content
    assert '某人' not in user_content


def test_schedule_agent_builds_a_life_instead_of_a_duty_roster() -> None:
    prompt = build_plan_prompt(
        date='2032-07-15',
        weekday='周四',
        occasion='没有特别节日',
        persona='今天有点累。',
        yesterday_theme='把没看完的短篇读完。',
        unfinished_intentions='把没看完的短篇读完（已经滚了 1 天）',
        density='最近偶尔聊聊。',
        character_name=TEST_NAME,
        character_personality=TEST_PERSONALITY,
    )

    assert '只是方向，不是活动时刻表' in prompt
    assert '活动方向和生活节奏必须服从角色设定' in prompt
    assert 'intentions 必须有 3 到 5 条' in prompt
    assert '不带时刻' in prompt


def test_persona_state_does_not_force_repetitive_care() -> None:
    prompt = describe_persona(PersonaState(
        intimacy=95,
        energy=10,
        mood=50.0,
        updated_at=0,
    ))

    assert '关系深度：深厚' in prompt
    assert '精力' not in prompt
    assert '困' not in prompt
    assert '反复确认他还在' not in prompt


def test_expression_render_uses_situation_style_template() -> None:
    """注入形态：每条选中样本渲染为「当“情境”时，可以用“说法”来表达。」。"""
    samples = [
        ExpressionSample(id=1, situation='对方在抱怨具体麻烦时', style='先接住那件麻烦'),
        ExpressionSample(id=2, situation='对方开玩笑时', style='顺着继续接'),
    ]
    block = render_expression_habits(samples)

    assert '【表达习惯参考，请视情况自然的使用】' in block
    assert '当“对方在抱怨具体麻烦时”时，可以用“先接住那件麻烦”来表达。' in block
    assert block.count('- ') == len(samples)
    assert render_expression_habits([]) == ''


def test_main_prompt_carries_expression_samples_and_attention_drift() -> None:
    prompt = _build_prompt(
        expression_habits=render_expression_habits([
            ExpressionSample(id=1, situation='对方在抱怨具体麻烦时', style='先接住那件麻烦'),
        ]),
        tone=TEST_TONES[0],
    )

    assert '# 表达方式参考' in prompt
    assert '对方在抱怨具体麻烦时' in prompt
    assert TEST_TONES[0] in prompt
    # 表达样本紧邻输出格式，避免边界文本和协议要求削弱其示范作用。
    assert prompt.index('# 表达方式参考') > prompt.index('# 说话风格')
    assert prompt.index('# 表达方式参考') < prompt.index('# 输出格式')


def test_optional_blocks_stay_out_when_nothing_selected() -> None:
    prompt = _build_prompt()

    assert '# 表达方式参考' not in prompt
    assert not any(tone in prompt for tone in TEST_TONES)


def test_tone_variation_is_occasional_not_every_turn() -> None:
    rng = random.Random(3)
    picked = [pick_tone(0.25, TEST_TONES, rng) for _ in range(200)]

    assert None in picked
    assert any(tone is not None for tone in picked)
    assert all(tone in TEST_TONES for tone in picked if tone is not None)
    assert pick_tone(0, TEST_TONES, rng) is None


def test_proactive_prompt_shows_how_to_open_not_only_what_to_avoid() -> None:
    prompt = build_proactive_prompt('基础人设', '他已经写了一阵代码。')

    assert '大概是这种起头方式' in prompt
    assert '也可以从你自己那边起头' in prompt


def test_summary_agent_keeps_a_specific_anchor_for_later_recall() -> None:
    from src.core.agent.summarize import _system_prompt

    prompt = _system_prompt('巡星', '你是沿星图迁徙的数据生命。')
    assert '具体锚点' in prompt
    assert '以后在什么情境下会需要想起这段' in prompt
    assert '沿星图迁徙的数据生命' in prompt


def test_vision_agent_returns_observation_instead_of_assumptions() -> None:
    """按 context 分四套提示词的实现已移除，只保留明确屏幕意图时使用的场景。
    提示词只描述当前视觉结果；无法识别时明确说明，不根据历史内容推测。"""
    prompt = VisionService._build_vision_prompt()

    assert '正在做什么' in prompt
    assert '认不出来就直说看不清' in prompt
    assert '不要猜' in prompt
