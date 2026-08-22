"""把回合动作空间翻译成 OpenAI 兼容的工具声明。

动作协议本身（动作枚举、理由码分域、目标范围、篇幅档位）由 ``action_protocol``
定义，本模块只负责换一种表达方式：同一套约束，从「XML 动作头 + 提示词文字」改写
成「函数签名 + JSON Schema」。两种表达必须同源，因此这里的枚举值全部引用
``action_protocol`` 的常量，不重新抄一份。

对外暴露 ``build_tool_definitions``（按回合帧生成工具列表）与
``decision_head_from_tool_call``（把模型选中的工具调用还原成 ``DecisionHead``），
被 ``ConversationAgent`` 在工具调用模式下使用。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, cast

import json

from .action_protocol import (
    COGNITIVE_ACTIONS,
    REPLY_REASON_CODES,
    SILENT_REASON_CODES,
    SPEAK_REASON_CODES,
    WAIT_REASON_CODES,
    ConversationAction,
    DecisionFrame,
    DecisionHead,
    IllegalActionError,
    ReplyLength,
)

# 每个动作的一句话说明。措辞与 XML 协议保持同一口径：说清什么时候用它，
# 而不是复述动作名。
_ACTION_DESCRIPTIONS: Dict[str, str] = {
    'reply': '对某条消息开口回应。只有真的要说话时才用它。',
    'silent': '这一轮不接话。别人正在聊自己的事、没有新东西可加、话题已经结束时用它。',
    'wait': '话还没说完，先不表态，等对方把话说完再决定。',
    'react': '不说话，只给某条消息贴一个表情回应。',
    'poke': '不说话，只戳一下某条消息的发送者。',
    'speak': '没有人在跟你说话，但你自己想起了什么想说。没料就别用。',
    'recall': '先查一下长期记忆里与某件事相关的内容，再决定这一轮做什么。',
    'inspect': '先查一下某个人的画像与最近互动，再决定这一轮做什么。',
}

# 理由码按动作分域，与 _validate_reason_codes 同一套划分。react 与 poke
# 都是「做出了回应」，与回复同域。
_REASON_DOMAINS: Dict[str, frozenset[str]] = {
    'reply': REPLY_REASON_CODES,
    'react': REPLY_REASON_CODES,
    'poke': REPLY_REASON_CODES,
    'speak': SPEAK_REASON_CODES,
    'silent': SILENT_REASON_CODES,
    'wait': WAIT_REASON_CODES,
}


def build_tool_definitions(frame: DecisionFrame) -> List[Dict[str, Any]]:
    """按回合帧生成本轮可用的工具声明。

    只声明动作集里真实存在的动作：声明一个本回合非法的工具等于主动制造
    ``illegal_action``，与 XML 协议里「示例按动作空间逐条开关」是同一条理由。

    :param frame: 本回合固定快照，提供动作集、可选消息与平台能力。
    :return: OpenAI 兼容的工具声明列表，按动作名排序保证请求可复现。
    """
    return [
        _tool_for(action, frame)
        for action in sorted(frame.available_actions)
    ]


def _tool_for(action: str, frame: DecisionFrame) -> Dict[str, Any]:
    """生成单个动作的工具声明。

    :param action: 动作名，同时作为工具名。
    :param frame: 本回合固定快照。
    :return: 单个 OpenAI 工具声明。
    """
    properties: Dict[str, Any] = {}
    required: List[str] = []

    if action in COGNITIVE_ACTIONS:
        properties['query'] = {
            'type': 'string',
            'description': '要查什么，一句话；写具体内容，不要写空泛的字眼。',
        }
        required.append('query')
    else:
        domain = _REASON_DOMAINS[action]
        properties['reasons'] = {
            'type': 'array',
            'items': {'type': 'string', 'enum': sorted(domain)},
            'minItems': 1,
            'description': '为什么做这个动作，只能从给定取值里选，不允许自造。',
        }
        required.append('reasons')

    if action in ('reply', 'react', 'poke'):
        properties['target'] = {
            'type': 'integer',
            'enum': list(frame.selectable_message_ids),
            'description': '这一轮针对的那条消息编号，只填一个，必须来自给定取值。',
        }
        required.append('target')

    if action in ('reply', 'speak'):
        properties['length'] = {
            'type': 'string',
            'enum': ['brief', 'long'],
            'description': (
                'brief 是省力口语的短接话，绝大多数时候都该选它；'
                'long 只在对方抛来要展开的问题时选。'
            ),
        }
        properties['reference'] = {
            'type': 'string',
            'description': (
                '交给下一环节写正文用的背景：为什么开口、在接谁的哪句话、有哪些前因。'
                '写事实与方向，不要写台词。'
            ),
        }
        required.extend(['length', 'reference'])

    if action == 'react':
        properties['reaction'] = {
            'type': 'string',
            'enum': list(frame.capabilities.available_reactions),
            'description': '要贴的表情，必须来自给定取值。',
        }
        required.append('reaction')

    if action == 'reply' and frame.capabilities.quote:
        properties['quote'] = {
            'type': 'integer',
            'enum': list(frame.selectable_message_ids),
            'description': '需要显式挂引用时填被引用的消息编号；不需要就不要填这个字段。',
        }

    return {
        'type': 'function',
        'function': {
            'name': action,
            'description': _ACTION_DESCRIPTIONS[action],
            'parameters': {
                'type': 'object',
                'properties': properties,
                'required': required,
                'additionalProperties': False,
            },
        },
    }


def decision_head_from_tool_call(
    name: str,
    arguments: str,
    frame: DecisionFrame,
) -> DecisionHead:
    """把模型选中的工具调用还原成已通过校验的动作头。

    与 XML 路径共用 ``DecisionHead`` 与 ``validate``：动作空间、目标范围、引用
    能力这些硬边界只有一份实现，换表达方式不等于换判据。

    :param name: 工具名，即动作名。
    :param arguments: 工具参数的 JSON 文本；空串按空对象处理。
    :param frame: 本回合固定快照。
    :return: 已完成结构与帧校验的 DecisionHead。
    :raises IllegalActionError: 参数不是合法 JSON、字段类型不符或未通过帧校验。
    """
    try:
        payload = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError as exc:
        raise IllegalActionError(f'工具 {name} 的参数不是合法 JSON：{exc}') from exc
    if not isinstance(payload, dict):
        raise IllegalActionError(f'工具 {name} 的参数必须是对象')

    normalized_name = name.strip()
    if normalized_name not in _ACTION_DESCRIPTIONS:
        raise IllegalActionError(f'未知动作：{normalized_name}')
    action = cast(ConversationAction, normalized_name)
    _validate_payload_shape(action, payload, frame)
    head = DecisionHead(
        action=action,
        target_message_ids=_target_id(payload.get('target')),
        quote_message_id=_optional_int(payload.get('quote'), 'quote'),
        reason_codes=_reason_codes(payload.get('reasons')),
        length=_length(payload.get('length')),
        query=_optional_text(payload.get('query')),
        reaction=_optional_text(payload.get('reaction')),
        reference=_optional_text(payload.get('reference')),
    )
    head.validate(frame)
    return head


def _validate_payload_shape(
    action: ConversationAction,
    payload: Dict[str, Any],
    frame: DecisionFrame,
) -> None:
    """按本轮工具声明拒绝未知字段与缺失的必填字段。

    工具服务商不一定替调用方执行 JSON Schema 校验，因此不能依赖 ``required``
    或 ``additionalProperties`` 自动生效。若字段名发生漂移，必须在这里直接指出
    未知字段或缺失字段，不能等动作头把它误报成「没有目标消息」。

    :param action: 已确认属于封闭动作集的工具名。
    :param payload: 已解析的工具参数对象。
    :param frame: 本回合固定快照，用于生成同源工具声明。
    :raises IllegalActionError: 动作不在本轮动作集、出现未知字段或缺少必填字段。
    """
    if action not in frame.available_actions:
        raise IllegalActionError(
            f'动作 {action} 不在本回合可用动作 {sorted(frame.available_actions)} 中'
        )
    parameters = _tool_for(action, frame)['function']['parameters']
    allowed = frozenset(parameters['properties'])
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise IllegalActionError(
            f'工具 {action} 收到未知字段：{", ".join(unknown)}'
        )
    missing = [field for field in parameters['required'] if field not in payload]
    if missing:
        raise IllegalActionError(
            f'工具 {action} 缺少必填字段：{", ".join(missing)}'
        )


def _target_id(raw: Any) -> tuple[int, ...]:
    """把单个 target 参数规范化为内部目标消息元组。

    工具协议只允许选择一条目标消息，因此数组属于形状错误；数字字符串仍接受，
    因为部分 OpenAI 兼容服务会把 JSON Schema 的 integer 参数序列化成字符串。
    取值范围仍由动作头的帧校验严格限制。

    :param raw: 工具参数里的 target 原值。
    :return: 消息编号元组；未提供时为空元组。
    :raises IllegalActionError: 取值不是可转成整数的单个标量。
    """
    if raw is None:
        return ()
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise IllegalActionError(f'target 必须是单个消息编号，收到 {raw!r}')
    try:
        return (int(raw),)
    except ValueError as exc:
        raise IllegalActionError(f'target 必须是单个消息编号，收到 {raw!r}') from exc


def _optional_int(raw: Any, field: str) -> int | None:
    """把可选整数字段规范化；缺省与空串都按未提供处理。

    :param raw: 工具参数原值。
    :param field: 字段名，用于错误消息。
    :return: 整数或 ``None``。
    :raises IllegalActionError: 取值不是可转成整数的标量。
    """
    if raw is None or raw == '':
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise IllegalActionError(f'{field} 必须是消息编号，收到 {raw!r}')
    try:
        return int(raw)
    except ValueError as exc:
        raise IllegalActionError(f'{field} 必须是消息编号，收到 {raw!r}') from exc


def _reason_codes(raw: Any) -> tuple[str, ...]:
    """把 reasons 参数规范化为字符串元组；分域校验交给动作头自检。

    :param raw: 工具参数里的 reasons 原值。
    :return: 理由码元组；未提供时为空元组。
    :raises IllegalActionError: 取值不是字符串。
    """
    if raw is None:
        return ()
    values: Sequence[Any] = raw if isinstance(raw, list) else [raw]
    codes: List[str] = []
    for value in values:
        if not isinstance(value, str):
            raise IllegalActionError(f'reasons 必须是字符串，收到 {value!r}')
        code = value.strip()
        if code:
            codes.append(code)
    return tuple(codes)


def _length(raw: Any) -> ReplyLength | None:
    """把 length 参数规范化；未知取值交由动作头自检拒绝。

    :param raw: 工具参数里的 length 原值。
    :return: 篇幅枚举值或 ``None``。
    :raises IllegalActionError: 取值不是字符串。
    """
    if raw is None or raw == '':
        return None
    if not isinstance(raw, str):
        raise IllegalActionError(f'length 必须是字符串，收到 {raw!r}')
    return cast(ReplyLength, raw.strip())


def _optional_text(raw: Any) -> str | None:
    """把可选文本字段规范化：空白按未提供处理。

    :param raw: 工具参数原值。
    :return: 去空白后的文本；为空时返回 ``None``。
    :raises IllegalActionError: 取值不是字符串。
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise IllegalActionError(f'字段必须是字符串，收到 {raw!r}')
    text = raw.strip()
    return text or None
