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


def fit_char_budget(messages: List[dict], budget: int = DEFAULT_CHAR_BUDGET) -> List[dict]:
    """按字符预算从最早消息开始裁剪，并重新规范化剩余历史。

    裁剪可能产生助手消息开头或连续同角色消息，因此裁剪完成后必须再次调用
    ``normalize_history``。

    :param messages: 已解析的消息字典列表；函数不会就地删除其中元素。
    :param budget: 允许保留的总字符数，默认 ``DEFAULT_CHAR_BUDGET``；小于等于 ``0``
            时直接返回原列表对象。

    :return: 在预算内且结构合法的新消息列表；输入为空时返回空列表。

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
    return normalize_history(kept)
