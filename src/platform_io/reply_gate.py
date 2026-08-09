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
    """一次确定性的群聊回复决策。"""

    accepted: bool
    reason: str


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
    if mentioned_me and at_mention_must_reply:
        return ReplyGateDecision(accepted=True, reason='mentioned')

    name_mentioned = mentions_bot_name(text, bot_names)
    if asleep:
        return ReplyGateDecision(accepted=False, reason='asleep_without_mention')
    if my_replies_in_window >= max_replies_in_window:
        return ReplyGateDecision(accepted=False, reason='window_limit')
    if mentioned_me or name_mentioned:
        if probability_draw < name_mention_probability:
            reason = 'mentioned_probability' if mentioned_me else 'name_mentioned'
            return ReplyGateDecision(accepted=True, reason=reason)
        reason = 'mention_probability' if mentioned_me else 'name_probability'
        return ReplyGateDecision(accepted=False, reason=reason)
    return ReplyGateDecision(accepted=False, reason='group_not_mentioned')


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
