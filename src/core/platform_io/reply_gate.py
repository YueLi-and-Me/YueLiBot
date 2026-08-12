"""根据群聊表面上下文和频率窗口决定是否进入回合处理。

本模块只处理协议 @、文本称呼、睡眠状态和窗口配额，不访问数据库，
因此可以由入口在接线时调用并将返回的判定依据写入 trace。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import unicodedata

from src.core.platform_io.types import StreamKind


_VOCATIVE_PREFIXES = frozenset('叫喊找问让喂@')
_VOCATIVE_SUFFIXES = frozenset('在来呢吗呀啊诶欸说看帮回醒你请给陪要能可好出')


@dataclass(frozen=True)
class ReplyGateDecision:
    """群聊回复决策及其可审计的判定输入。

    布尔字段记录触发条件，计数字段记录频率窗口，便于还原门控决策。
    """

    accepted: bool
    reason: str
    asleep: bool = False
    mentioned_me: bool = False
    name_mentioned: bool = False
    replies_in_window: int = 0
    max_replies_in_window: int = 0

    def as_trace(self) -> dict:
        """将决策转换为观察事件使用的字典。

        :return: 使用项目 trace 字段命名约定的可序列化字典。
        """
        return {
            'accepted': self.accepted,
            'reason': self.reason,
            'asleep': self.asleep,
            'mentionedMe': self.mentioned_me,
            'nameMentioned': self.name_mentioned,
            'repliesInWindow': self.replies_in_window,
            'maxRepliesInWindow': self.max_replies_in_window,
        }


def decide_reply(
    stream_kind: StreamKind,
    asleep: bool,
    mentioned_me: bool,
    text: str,
    bot_names: Sequence[str],
    at_mention_must_reply: bool,
    my_replies_in_window: int,
    max_replies_in_window: int,
) -> ReplyGateDecision:
    """按群聊规则计算一次是否回复的判定结果。

    :param stream_kind: 会话类型；非 ``group`` 时直接允许回复。
    :param asleep: 当前主体是否处于睡眠状态。
    :param mentioned_me: 协议层是否明确 @ 主体。
    :param text: 待分析的消息正文。
    :param bot_names: 可被正文称呼匹配的主体名称序列。
    :param at_mention_must_reply: 为 ``True`` 时，明确 @ 直接绕过睡眠和窗口限制。
    :param my_replies_in_window: 当前频率窗口内已经发送的回复数。
    :param max_replies_in_window: 当前窗口允许的最大回复数。

    :return: 包含接受结果、原因和完整判定输入的 ``ReplyGateDecision``。

    :raises ValueError: 名称序列包含空字符串。
    """
    # 非群聊不受群聊门控约束，直接保留普通对话路径。
    if stream_kind != 'group':
        return ReplyGateDecision(accepted=True, reason='not_group')
    # 先完成文本称呼识别，后续所有分支复用同一判定，避免重复扫描正文。
    name_mentioned = mentions_bot_name(text, bot_names)

    def decision(accepted: bool, reason: str) -> ReplyGateDecision:
        """使用当前输入构造带完整审计字段的判定结果。

        :param accepted: 是否允许本次回复。
        :param reason: 稳定的机器可读判定原因。

        :return: 填充当前群聊上下文和计数字段的决策对象。
        """

        return ReplyGateDecision(
            accepted=accepted,
            reason=reason,
            asleep=asleep,
            mentioned_me=mentioned_me,
            name_mentioned=name_mentioned,
            replies_in_window=my_replies_in_window,
            max_replies_in_window=max_replies_in_window,
        )

    # 协议 @ 的强制回复优先级最高，必须在睡眠和窗口限制前处理。
    if mentioned_me and at_mention_must_reply:
        return decision(True, 'mentioned')
    if asleep:
        return decision(False, 'asleep_without_mention')
    if my_replies_in_window >= max_replies_in_window:
        return decision(False, 'window_limit')
    if mentioned_me or name_mentioned:
        # 非必回 @ 与文本称呼只负责放入回合；实际概率由上下文后的动作策略决定。
        return decision(True, 'deferred_to_action_policy')
    return decision(False, 'group_not_mentioned')


def mentions_bot_name(text: str, bot_names: Sequence[str]) -> bool:
    """判断正文是否直接包含主体名称。

    :param text: 待匹配的消息正文。
    :param bot_names: 可用名称序列；每个名称必须非空，比较时忽略大小写。

    :return: 发现满足中英文边界规则的名称时返回 ``True``，否则返回 ``False``。

    :raises ValueError: ``bot_names`` 包含空字符串。
    :raises TypeError: 输入元素不支持字符串操作时由 Python 直接抛出。
    """
    normalized_text = text.casefold()
    for raw_name in bot_names:
        name = raw_name.strip().casefold()
        if not name:
            raise ValueError('bot_names 不能包含空字符串')
        start = normalized_text.find(name)
        while start >= 0:
            end = start + len(name)
            if _is_name_boundary(normalized_text, start, end, name):
                return True
            start = normalized_text.find(name, start + 1)
    return False


def _is_name_boundary(text: str, start: int, end: int, name: str) -> bool:
    """判断名称两侧是否满足中文或 ASCII 单词边界规则。

    :param text: 已规范化的正文。
    :param start: 名称匹配的起始索引。
    :param end: 名称匹配的结束索引（不包含）。
    :param name: 已规范化的名称。

    :return: 名称不嵌入其他 ASCII 标识符且符合中文称呼边界时返回 ``True``。
    """

    before = text[start - 1] if start > 0 else ''
    after = text[end] if end < len(text) else ''
    if not all(character.isascii() for character in name):
        before_ok = (
            not before
            or _is_separator(before)
            or before in _VOCATIVE_PREFIXES
        )
        after_ok = (
            not after
            or _is_separator(after)
            or after in _VOCATIVE_SUFFIXES
        )
        return before_ok and after_ok
    return not _is_ascii_identifier(before) and not _is_ascii_identifier(after)


def _is_ascii_identifier(character: str) -> bool:
    """判断字符是否属于 ASCII 标识符字符。

    :param character: 待判断的单字符字符串；空字符串表示文本边界。

    :return: 字符为 ASCII 字母、数字或下划线时返回 ``True``。
    """

    return bool(character) and character.isascii() and (
        character.isalnum() or character == '_'
    )


def _is_separator(character: str) -> bool:
    """判断字符是否为空白或 Unicode 标点/符号分隔符。

    :param character: 待判断的单字符字符串。

    :return: 字符可作为名称边界分隔符时返回 ``True``。
    """

    return character.isspace() or unicodedata.category(character).startswith(('P', 'S'))
