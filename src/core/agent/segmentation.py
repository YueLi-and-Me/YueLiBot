"""把一条可见台词切成符合打字习惯的多个气泡。

模型按语义边界写 ``<say>``，一个 ``<say>`` 常常仍是完整的一长句；真人在聊天
窗口里会把这样的一句拆成两三条陆续发出去。本模块只做这一步纯文本切分，不参与
协议解析，也不决定发送节奏。

被 ``src.core.services.chat._collect_outbound_segment`` 在收束 ``<say>`` 边界
时调用，切分结果同时进入平台投递、助手历史和控制台渲染，三者看到的气泡完全
一致。
"""

from __future__ import annotations

from math import ceil
from typing import List

# 单条气泡的目标字数：先按标点切成最小片段，再贪心合并到接近该长度为止。用连续
# 的长度阈值而不是分档概率，是为了让同一句话的切法稳定可复现——句子内容本身每轮
# 都在变，气泡边界不需要再叠一层随机。
BUBBLE_TARGET_CHARS = 18

# 一条台词最多切成几条气泡。人的打字习惯是话越多每条也越长，而不是条数无限增长；
# 没有这个上限时一段长台词会碎成七八条，读起来是刷屏而不是聊天。两个常量分管两端
# 且互不牵制：短台词由目标字数决定切不切，长台词由条数上限反推每条该多长。
MAX_BUBBLES_PER_SAY = 3

# 断句标点。命中后连同其后连续的同类标点一起归入前一片段，避免「？！」被拆散。
_BREAK_MARKS = frozenset('，,、；;。！!？?…～~\n')

# 气泡结尾不该留下的延续性标点：真人不会用逗号收尾一条消息。终止性标点
# （。？！等）属于语气，保留原样，由人格提示词决定要不要写。
_TRAILING_MARKS = '，,、；; \t'


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


def split_into_bubbles(text: str) -> List[str]:
    """把一条台词切成若干条可独立发送的气泡。

    切分不会把一个最小片段再切开：片段本身超长时宁可留成一条长气泡，也不在
    句子中间硬断。因此本函数只能改善「模型写了长句」的观感，压不住篇幅本身，
    篇幅由提示词的 length 规则负责。

    :param text: 单条 ``<say>`` 的完整正文；调用方应已去除首尾空白。
    :return: 至少一条的气泡列表；输入为空白时返回空列表。
    """
    stripped = text.strip()
    if not stripped:
        return []
    # 台词越长，单条气泡的目标也按比例放宽，使条数收敛在上限附近。
    target = max(BUBBLE_TARGET_CHARS, ceil(len(stripped) / MAX_BUBBLES_PER_SAY))
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
