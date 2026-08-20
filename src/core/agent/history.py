"""在组装模型上下文前规范化历史消息，修复中断和旧数据造成的结构不完整。

本模块在读取侧执行幂等清理，处理未闭合的 ``<say>`` 标签、应从上下文移除的
副作用标签、连续同角色消息和字符预算。清理结果只用于模型请求，不修改持久化
历史；因此既能修复既有记录，也不会让上下文格式依赖写入端始终成功。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Mapping

# 副作用标签只供后端解析，不应再次注入模型上下文。
# memory、mood 和 think 标签属于已执行的后端动作；重复注入会诱导模型重复输出。
# <say> 保留，因为它属于输出协议示例，移除后模型可能误判纯文本为唯一合法格式。
_SIDE_EFFECT_TAGS = re.compile(
    r'<(memory|mood|think|thinking)\b[^>]*>[\s\S]*?</\1>|<(memory|mood)\b[^>]*/?>',
    re.IGNORECASE,
)

_OPEN_SAY = re.compile(r'<say\b[^>]*>', re.IGNORECASE)
_CLOSE_SAY = re.compile(r'</say\s*>', re.IGNORECASE)
# Agent 模式的输出协议是「先 <decision> 再 <say>」；历史里的 <say> 标签会诱导
# 模型继续以 <say> 开头，因此读入 Agent 上下文时只保留可见台词。
_SAY_TAGS = re.compile(r'</?say\b[^>]*>', re.IGNORECASE)

# 单轮历史的字符预算。仅按消息条数限制无法约束长文本，因此按最早消息顺序裁剪。
DEFAULT_CHAR_BUDGET = 12_000


def strip_side_effect_tags(raw: str) -> str:
    """移除只供后端执行的副作用标签，保留 ``<say>`` 及其正文。

    :param raw: 待清理的模型原始文本；空字符串和空白文本返回空字符串。

    :return: 删除 ``memory``、``mood``、``think`` 和 ``thinking`` 标签后的文本，
        并去除首尾空白。

    :raises TypeError: ``raw`` 不是可用于正则替换的字符串时由正则操作触发。
    """

    return _SIDE_EFFECT_TAGS.sub('', raw or '').strip()


def close_dangling_say(raw: str) -> str:
    """补齐流式中断导致的未闭合 ``<say>`` 标签。

    读取侧必须保留已产生的文本，同时恢复后续上下文所需的标签结构；因此只补充
    缺失的闭合标签，不截断原始内容。

    :param raw: 可能包含未闭合 ``<say>`` 标签的原始文本。

    :return: 已去除首尾空白且标签闭合的文本；空输入返回空字符串。

    :raises TypeError: ``raw`` 不是字符串时由正则匹配操作触发。
    """

    text = (raw or '').strip()
    if not text:
        return ''
    unclosed = len(_OPEN_SAY.findall(text)) - len(_CLOSE_SAY.findall(text))
    if unclosed > 0:
        text += '</say>' * unclosed
    return text


def strip_say_tags(raw: str) -> str:
    """移除历史回复中的 ``<say>`` 外壳，只保留可见台词。

    普通对话提示词需要保留 ``<say>`` 作为输出示例；Conversation Agent 的输出
    协议以 ``<decision>`` 开头，历史中大量 ``<say>`` 开头会让模型模仿旧格式，
    因此 Agent 上下文单独调用本函数做纯文本化。

    :param raw: 已去除副作用标签的助手回复文本。
    :return: 仅含台词内容的纯文本；相邻 ``<say>`` 段之间以换行分隔，
        避免多条气泡的台词被拼成一句连读。
    :raises TypeError: ``raw`` 不是字符串时由正则操作触发。
    """
    parts = [part.strip() for part in _SAY_TAGS.split(raw or '') if part.strip()]
    return '\n'.join(parts)


def normalize_history(messages: Iterable[Mapping[str, str]]) -> List[dict]:
    """将任意来源的历史消息规范化为模型端点可接受的结构。

    1. assistant 内容剥副作用标签、补悬空 <say>；
    2. 丢掉清理后变空的消息；
    3. 合并连续同角色消息——这正是历史被打断损坏后的形态（连着两条 user），
       部分 OpenAI 兼容端点会直接拒收；
    4. 丢掉开头的 assistant——system 之后必须由 user 起头。

    该操作具有幂等性：对已规范化的历史重复执行不会改变结果。

    :param messages: 消息迭代器；每项至少提供字符串 ``role`` 和 ``content`` 字段。

    :return: 删除空消息、修复助手标签、合并连续角色并移除首个助手消息后的新列表。

    :raises TypeError: 消息项不支持映射访问或字段值不支持字符串处理时抛出。

    副作用：
        消费输入迭代器；不修改输入映射对象，仅创建新的消息字典。
    """

    cleaned: List[dict] = []
    for message in messages:
        role = message.get('role') or ''
        content = message.get('content') or ''
        if role == 'assistant':
            content = close_dangling_say(strip_side_effect_tags(content))
        else:
            content = content.strip()
        if not content:
            continue
        if cleaned and cleaned[-1]['role'] == role:
            cleaned[-1]['content'] = f"{cleaned[-1]['content']}\n{content}"
            continue
        cleaned.append({'role': role, 'content': content})

    while cleaned and cleaned[0]['role'] == 'assistant':
        cleaned.pop(0)
    return cleaned


def fit_char_budget(
    messages: List[dict],
    budget: int = DEFAULT_CHAR_BUDGET,
    *,
    preserve_items: bool = False,
) -> List[dict]:
    """按字符预算从最早消息开始裁剪，并按消息模式收尾。

    传统角色历史在裁剪后可能以 assistant 开头，或者留下连续同角色消息，因此
    默认再次调用 ``normalize_history``。扁平 item 流里每一项本来就都是独立的
    ``user`` 消息；这时合并同角色会把时间、画像、历史和工具提示重新糊成一块，
    调用方必须显式传入 ``preserve_items=True`` 保留边界。

    :param messages: 已解析的消息字典列表；函数不会就地删除其中元素。
    :param budget: 允许保留的总字符数，默认 ``DEFAULT_CHAR_BUDGET``；小于等于 ``0``
            时直接返回原列表对象。
    :param preserve_items: 是否保留裁剪后的消息边界与角色，不再执行角色规范化。

    :return: 在预算内的新消息列表；传统模式还会修复角色结构，item 模式保持
        每个剩余项原样独立。输入为空时返回空列表。

    :raises KeyError: 消息缺少 ``content`` 字段时抛出。
    :raises TypeError: 内容不是支持 ``len`` 的对象或预算不可比较时抛出。

    副作用：
        当 ``budget <= 0`` 时返回输入列表本身；其他情况不修改输入列表。
    """

    if budget <= 0:
        return messages
    kept = list(messages)
    total = sum(len(m['content']) for m in kept)
    while kept and total > budget:
        total -= len(kept[0]['content'])
        kept.pop(0)
    return kept if preserve_items else normalize_history(kept)
