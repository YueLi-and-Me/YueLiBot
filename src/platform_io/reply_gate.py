"""群聊回复门控：纯函数，供入口在接线时记录决策。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import unicodedata

from src.platform_io.types import StreamKind


_VOCATIVE_PREFIXES = frozenset('叫喊找问让喂@')
_VOCATIVE_SUFFIXES = frozenset('在来呢吗呀啊诶欸说看帮回醒你请给陪要能可好出')


@dataclass(frozen=True)
class ReplyGateDecision:
    """群聊回复决策及判定依据。"""

    accepted: bool
    reason: str
    asleep: bool = False
    mentioned_me: bool = False
    name_mentioned: bool = False
    replies_in_window: int = 0
    max_replies_in_window: int = 0
    probability_draw: float = 0.0
    name_mention_probability: float = 0.0

    def as_trace(self) -> dict:
        """转换为 trace 字段。"""
        return {
            'accepted': self.accepted,
            'reason': self.reason,
            'asleep': self.asleep,
            'mentionedMe': self.mentioned_me,
            'nameMentioned': self.name_mentioned,
            'repliesInWindow': self.replies_in_window,
            'maxRepliesInWindow': self.max_replies_in_window,
            'probabilityDraw': round(self.probability_draw, 4),
            'nameMentionProbability': self.name_mention_probability,
        }


def decide_reply(
    stream_kind: StreamKind,
    asleep: bool,
    mentioned_me: bool,
    text: str,
    bot_names: Sequence[str],
    at_mention_must_reply: bool,
    name_mention_probability: float,
    probability_draw: float,
    my_replies_in_window: int,
    max_replies_in_window: int,
) -> ReplyGateDecision:
    """区分协议 @ 与文本称呼，并在群聊频率限制前处理“@ 必回”。"""
    if stream_kind != 'group':
        return ReplyGateDecision(accepted=True, reason='not_group')
    if not 0.0 <= name_mention_probability <= 1.0:
        raise ValueError('name_mention_probability 必须在 0 到 1 之间')
    if not 0.0 <= probability_draw < 1.0:
        raise ValueError('probability_draw 必须在 0 到 1 之间且不含 1')

    name_mentioned = mentions_bot_name(text, bot_names)

    def decision(accepted: bool, reason: str) -> ReplyGateDecision:
        return ReplyGateDecision(
            accepted=accepted,
            reason=reason,
            asleep=asleep,
            mentioned_me=mentioned_me,
            name_mentioned=name_mentioned,
            replies_in_window=my_replies_in_window,
            max_replies_in_window=max_replies_in_window,
            probability_draw=probability_draw,
            name_mention_probability=name_mention_probability,
        )

    if mentioned_me and at_mention_must_reply:
        return decision(True, 'mentioned')
    if asleep:
        return decision(False, 'asleep_without_mention')
    if my_replies_in_window >= max_replies_in_window:
        return decision(False, 'window_limit')
    if mentioned_me or name_mentioned:
        if probability_draw < name_mention_probability:
            return decision(True, 'mentioned_probability' if mentioned_me else 'name_mentioned')
        return decision(False, 'mention_probability' if mentioned_me else 'name_probability')
    return decision(False, 'group_not_mentioned')


def mentions_bot_name(text: str, bot_names: Sequence[str]) -> bool:
    """判断正文是否直接叫了机器人名字；英文名避免匹配到更长单词内部。"""
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
    return bool(character) and character.isascii() and (
        character.isalnum() or character == '_'
    )


def _is_separator(character: str) -> bool:
    return character.isspace() or unicodedata.category(character).startswith(('P', 'S'))
