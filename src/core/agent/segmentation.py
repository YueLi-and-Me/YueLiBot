"""把一条可见台词切成气泡，并给出每条气泡发出前的打字停顿。

模型按语义边界写 ``<say>``，一个 ``<say>`` 常常仍是完整的一长句；真人在聊天
窗口里会把这样的一句拆成两三条陆续发出去，而且每条之间隔着实打实的打字时间。
本模块把这两件事放在一起：切分决定发几条，打字停顿决定隔多久，二者共用同一套
「她怎么把一段话打出来」的假设。模块只做纯计算，不执行 I/O；全部数量级参数由
``src.core.config.schema.TypingConfig`` 提供，不在此处写死。

``split_into_bubbles`` 由 ``src.core.services.chat._collect_outbound_segment``
在收束 ``<say>`` 边界时调用；``typing_delay_seconds`` 由同一服务在组装出站消息
时逐条调用，结果随出站载荷下发给平台适配器执行，适配器不重算节奏。
"""

from __future__ import annotations

from math import ceil
from typing import List

from src.core.config.schema import TypingConfig

# 断句标点。命中后连同其后连续的同类标点一起归入前一片段，避免「？！」被拆散。
_BREAK_MARKS = frozenset('，,、；;。！!？?…～~\n')

# 气泡结尾不该留下的延续性标点：真人不会用逗号收尾一条消息。终止性标点
# （。？！等）属于语气，保留原样，由人格提示词决定要不要写。
_TRAILING_MARKS = '，,、；; \t'

# 中日韩统一表意文字区间，用于区分中文与拉丁字符的输入速度。
_CJK_FIRST = '一'
_CJK_LAST = '鿿'


def _is_ascii_word_char(char: str) -> bool:
    """判断字符是否属于英文单词内部字符，用于保护小数点与英文缩写。"""

    return char.isascii() and (char.isalnum() or char == '_')


def _split_fragments(text: str) -> List[str]:
    """按断句标点把整段文本切成最小片段，标点归入其左侧片段。

    ``.`` 只有在两侧都不是英文单词字符时才作为断点，否则 ``v4.0``、``3.5``
    这类内容会被切碎。

    :param text: 单条 ``<say>`` 的完整正文。
    :return: 顺序保持不变的片段列表；无断点时返回单元素列表。
    """
    fragments: List[str] = []
    buffer: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        buffer.append(char)
        index += 1
        if char == '.':
            previous = text[index - 2] if index >= 2 else ''
            following = text[index] if index < length else ''
            if _is_ascii_word_char(previous) and _is_ascii_word_char(following):
                continue
        elif char not in _BREAK_MARKS:
            continue
        # 连续标点属于同一个语气单位，一并吃掉再断开。
        while index < length and (text[index] in _BREAK_MARKS or text[index] == '.'):
            buffer.append(text[index])
            index += 1
        fragments.append(''.join(buffer))
        buffer = []
    if buffer:
        fragments.append(''.join(buffer))
    return fragments


def split_into_bubbles(text: str, typing: TypingConfig) -> List[str]:
    """把一条台词切成若干条可独立发送的气泡。

    切分不会把一个最小片段再切开：片段本身超长时宁可留成一条长气泡，也不在
    句子中间硬断。因此本函数只能改善「模型写了长句」的观感，压不住篇幅本身，
    篇幅由提示词的 length 规则负责。

    切法是确定性的，不掺随机：句子内容本身每轮都在变，气泡边界不需要再叠一层
    随机才显得自然，而确定性让同一条台词的切法可复现、可断言。

    :param text: 单条 ``<say>`` 的完整正文；调用方应已去除首尾空白。
    :param typing: 打字节奏配置，提供目标字数与条数上限。
    :return: 至少一条的气泡列表；输入为空白时返回空列表。
    """
    stripped = text.strip()
    if not stripped:
        return []
    # 台词越长，单条气泡的目标也按比例放宽，使条数收敛在上限附近。
    target = max(
        typing.bubble_target_chars,
        ceil(len(stripped) / typing.max_bubbles_per_say),
    )
    bubbles: List[str] = []
    current = ''
    for fragment in _split_fragments(stripped):
        if current and len(current) + len(fragment) > target:
            bubbles.append(current)
            current = fragment
        else:
            current += fragment
    if current:
        bubbles.append(current)
    trimmed = [bubble.rstrip(_TRAILING_MARKS).strip() for bubble in bubbles]
    return [bubble for bubble in trimmed if bubble]


def typing_delay_seconds(text: str, typing: TypingConfig) -> float:
    """估算把一条气泡打出来所需的时间。

    调用方只应对第二条及之后的气泡等待：模型生成本身已经占用了十几秒，第一条
    发出时她在对方视角里已经「打了很久」，再等一次会变成明显的迟钝。

    :param text: 即将发送的气泡正文。
    :param typing: 打字节奏配置，提供中英文速度、发送间隙与等待上限。
    :return: 建议的等待秒数；停顿关闭或文本为空时返回 ``0.0``。
    """
    if not text or not typing.delay_enabled:
        return 0.0
    seconds = typing.send_gap_seconds
    for char in text:
        seconds += (
            typing.chinese_char_seconds
            if _CJK_FIRST <= char <= _CJK_LAST
            else typing.latin_char_seconds
        )
    return min(seconds, typing.max_delay_seconds)
