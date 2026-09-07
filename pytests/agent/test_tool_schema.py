"""动作空间到工具声明的翻译回归。

工具声明与 XML 动作头是同一套约束的两种表达，这里盯住三件事：
只声明本回合合法的动作、参数取值与理由码分域一致、工具调用能还原成通过校验的动作头，
以及被拒绝时的原因文案带上可用取值——该文案会原样回灌给模型用于纠错重发。
"""

from __future__ import annotations

from typing import Any

import json
import pytest

from src.core.agent.action_protocol import (
    DecisionFrame,
    IllegalActionError,
    PlatformCapabilities,
    available_actions,
)
from src.core.agent.tool_schema import build_tool_definitions, decision_head_from_tool_call


def _caps(**overrides: Any) -> PlatformCapabilities:
    return PlatformCapabilities(**overrides)


def _frame(**overrides: Any) -> DecisionFrame:
    caps = overrides.pop('capabilities', _caps())
    base: dict[str, Any] = dict(
        turn_id=7,
        snapshot_id='snap-7',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101, 102),
        message_watermark=102,
        available_actions=available_actions('group', 'deliberate', caps),
        capabilities=caps,
    )
    base.update(overrides)
    return DecisionFrame(**base)


def _tool(tools: list[dict], name: str) -> dict:
    return next(item['function'] for item in tools if item['function']['name'] == name)


def test_only_available_actions_are_declared() -> None:
    """声明一个本回合非法的工具等于主动制造 illegal_action。"""
    frame = _frame()

    names = {item['function']['name'] for item in build_tool_definitions(frame)}

    assert names == set(frame.available_actions)
    # 平台没有验证表情能力时 react 不该出现。
    assert 'react' not in {
        item['function']['name'] for item in build_tool_definitions(
            _frame(capabilities=_caps(react=False))
        )
    }


def test_target_enum_matches_selectable_messages() -> None:
    """目标取值直接写进 enum，模型没有机会填一个不存在的编号。

    编号声明为字符串：Google 系的 function declaration 只允许 STRING 带 enum，
    整数枚举会让经该格式转换的候选模型固定返回 HTTP 400。
    """
    tools = build_tool_definitions(_frame())

    parameters = _tool(tools, 'reply')['parameters']

    assert parameters['properties']['target']['type'] == 'string'
    assert parameters['properties']['target']['enum'] == ['101', '102']
    assert parameters['additionalProperties'] is False


def test_reason_enum_is_domain_scoped() -> None:
    """理由码按动作分域，回复与沉默的取值集合不允许互串。"""
    tools = build_tool_definitions(_frame())

    reply_reasons = _tool(tools, 'reply')['parameters']['properties']['reasons']['items']['enum']
    silent_reasons = _tool(tools, 'silent')['parameters']['properties']['reasons']['items']['enum']

    assert 'can_add_value' in reply_reasons and 'can_add_value' not in silent_reasons
    assert 'no_new_value' in silent_reasons and 'no_new_value' not in reply_reasons


def test_quote_only_declared_when_platform_supports_it() -> None:
    """平台不支持显式引用时不声明该参数，避免模型填一个发不出去的字段。"""
    with_quote = build_tool_definitions(_frame(capabilities=_caps(quote=True)))
    without_quote = build_tool_definitions(_frame(capabilities=_caps(quote=False)))

    assert 'quote' in _tool(with_quote, 'reply')['parameters']['properties']
    assert 'quote' not in _tool(without_quote, 'reply')['parameters']['properties']


def test_tool_call_becomes_validated_head() -> None:
    """工具调用还原出的动作头与 XML 路径走同一套校验。"""
    frame = _frame()

    head = decision_head_from_tool_call(
        'reply',
        '{"target": 101, "reasons": ["can_add_value"], "length": "brief", '
        '"reference": "他在吐槽引擎，接一句"}',
        frame,
    )

    assert head.action == 'reply'
    assert head.target_message_ids == (101,)
    assert head.reason_codes == ('can_add_value',)
    assert head.length == 'brief'
    assert head.reference == '他在吐槽引擎，接一句'


def test_tool_call_accepts_numeric_string_target() -> None:
    """兼容服务商把 integer 参数序列化为字符串，但仍只接受一个目标。"""
    frame = _frame()

    head = decision_head_from_tool_call(
        'reply',
        '{"target": "102", "reasons": ["direct_question"], "length": "brief", '
        '"reference": "回他"}',
        frame,
    )

    assert head.target_message_ids == (102,)


def test_out_of_range_target_is_rejected() -> None:
    """越界目标必须被拒，这条硬边界不因换了表达方式而放松。"""
    frame = _frame()

    with pytest.raises(IllegalActionError):
        decision_head_from_tool_call(
            'reply',
            '{"target": 999, "reasons": ["can_add_value"], "length": "brief", '
            '"reference": "x"}',
            frame,
        )


def test_cross_domain_reason_is_rejected() -> None:
    """沉默理由不能用在回复上，分域是账本可读的前提。"""
    frame = _frame()

    with pytest.raises(IllegalActionError):
        decision_head_from_tool_call(
            'reply',
            '{"target": 101, "reasons": ["no_new_value"], "length": "brief", '
            '"reference": "x"}',
            frame,
        )


def test_malformed_arguments_are_rejected() -> None:
    """参数不是合法 JSON 时如实失败，不猜模型想说什么。"""
    frame = _frame()

    with pytest.raises(IllegalActionError, match='不是合法 JSON'):
        decision_head_from_tool_call('reply', '{"target": 101,', frame)


def test_tool_call_rejects_legacy_targets_field_precisely() -> None:
    """字段名漂移必须直接暴露，不能被误报成动作没有目标。"""
    frame = _frame()

    with pytest.raises(IllegalActionError, match='未知字段：targets'):
        decision_head_from_tool_call(
            'reply',
            '{"targets": 101, "reasons": ["can_add_value"], "length": "brief", '
            '"reference": "旧字段"}',
            frame,
        )


def test_tool_call_rejects_missing_target_precisely() -> None:
    """缺少目标字段时报告工具参数错误，而不是落到动作头的泛化报错。"""
    frame = _frame(
        capabilities=_caps(react=True, available_reactions=('赞',)),
    )

    with pytest.raises(IllegalActionError, match='缺少必填字段：target'):
        decision_head_from_tool_call(
            'react',
            '{"reasons": ["natural_reaction"], "reaction": "赞"}',
            frame,
        )


def test_tool_call_rejects_target_array() -> None:
    """工具模式每次只允许一个目标，数组不能再作为隐式兼容形状。"""
    frame = _frame()

    with pytest.raises(IllegalActionError, match='target 必须是单个消息编号'):
        decision_head_from_tool_call(
            'reply',
            '{"target": [101], "reasons": ["can_add_value"], "length": "brief", '
            '"reference": "数组目标"}',
            frame,
        )


def test_cognitive_tool_requires_query_only() -> None:
    """认知动作的审计信息是 query 本身，不要求理由码。"""
    caps = _caps()
    frame = _frame(
        available_actions=available_actions(
            "group", "deliberate", caps, cognitive_rounds_left=2,
        ),
    )
    tools = build_tool_definitions(frame)

    recall = _tool(tools, 'recall')['parameters']
    assert recall['required'] == ['query']

    head = decision_head_from_tool_call('recall', '{"query": "上次说的显卡"}', frame)
    assert head.action == 'recall' and head.query == '上次说的显卡'


def test_free_text_reason_is_rejected_with_the_available_values() -> None:
    """理由码写成散文时，拒绝原因必须列出该动作的可用取值。

    这条文案会被工具调用纠错原样回灌给模型。只说「不允许自由字符串」等于让它
    再猜一次，实测模型会换一句散文再被拒，一次纠错预算就此空耗。
    """
    frame = _frame()
    prose = '已对哥发布的开源项目表达过恭喜，对方尚未回复，可能正在忙于项目发布的相关事宜。'

    with pytest.raises(IllegalActionError) as excinfo:
        decision_head_from_tool_call(
            'reply',
            json.dumps(
                {
                    'target': 101,
                    'reasons': [prose],
                    'length': 'brief',
                    'reference': '散文理由',
                },
                ensure_ascii=False,
            ),
            frame,
        )

    message = str(excinfo.value)
    assert 'directly_addressed' in message and 'topic_continuation' in message
    # 整段散文不得原样进错误信息：它同时是控制台错误框与纠错回灌的正文。
    assert prose not in message
    assert prose[:10] in message, '截断后仍要能认出模型填了什么'


def test_cross_domain_reason_names_the_target_action_values() -> None:
    """分域不匹配时给出目标动作自己的取值，而不是只说不能组合。"""
    frame = _frame()

    with pytest.raises(IllegalActionError) as excinfo:
        decision_head_from_tool_call(
            'silent',
            '{"reasons": ["directly_addressed"]}',
            frame,
        )

    message = str(excinfo.value)
    assert 'not_addressed' in message and 'others_conversation' in message
    # 回复域的取值不能出现在沉默动作的指引里，否则模型会照着再填一次错的。
    assert 'topic_continuation' not in message
