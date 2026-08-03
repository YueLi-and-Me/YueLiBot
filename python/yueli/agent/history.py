"""回灌历史的读时修复。

借鉴 MaiBot 的 `src/maisaka/context/post_processor.py`：它把「历史结构可能是
坏的」当成常态，每一轮组装请求前都跑一遍 `_normalize_history_structure`，
而不是指望写入端永远不出错。这个取向对我们尤其重要——

  · 中断、崩溃、进程被杀，都会在写入端留下半截历史；
  · 更关键的是，用户库里**已经存在**的历史就是坏的（每一次打断都损坏了
    一轮），只修写入端救不回已经写坏的部分。

所以这里做的是幂等的读时清理：无论历史怎么来的，送进模型之前都先修成
一个结构合法、语义干净的序列。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Mapping

# 副作用标签：它们是给后端解析用的，不该回灌给模型看。
# 每轮都让模型看见 N 个「我上轮又记了一条」的范例，会直接对抗系统提示词里
# 「已经记过的内容不要写」。<say> 保留——它是输出协议的 few-shot，剥掉会让
# 模型以为纯文本也是合法输出。
_SIDE_EFFECT_TAGS = re.compile(
    r'<(memory|mood|think|thinking)\b[^>]*>[\s\S]*?</\1>|<(memory|mood)\b[^>]*/?>',
    re.IGNORECASE,
)

_OPEN_SAY = re.compile(r'<say\b[^>]*>', re.IGNORECASE)
_CLOSE_SAY = re.compile(r'</say\s*>', re.IGNORECASE)

# 单轮历史的字符预算。40 条短消息和 40 条长消息的体量能差一个量级，
# 只按条数封顶挡不住上下文膨胀，所以再叠一道字符预算，从最老的开始丢。
DEFAULT_CHAR_BUDGET = 12_000


def strip_side_effect_tags(raw: str) -> str:
    """剥掉 <memory>/<mood>/<think>，保留 <say> 及其内容。"""

    return _SIDE_EFFECT_TAGS.sub('', raw or '').strip()


def close_dangling_say(raw: str) -> str:
    """补齐未闭合的 <say>。

    中断发生在流式过程中间时，`assistant_raw` 很可能停在一个没闭合的 <say>
    上。这段内容用户已经在屏幕上看到了，不能丢；但直接入库会让后续每一轮
    都读到一个坏掉的标签。补齐而不是截断，是因为「用户看到过」比「结构好看」
    更重要。
    """

    text = (raw or '').strip()
    if not text:
        return ''
    unclosed = len(_OPEN_SAY.findall(text)) - len(_CLOSE_SAY.findall(text))
    if unclosed > 0:
        text += '</say>' * unclosed
    return text


def normalize_history(messages: Iterable[Mapping[str, str]]) -> List[dict]:
    """把任意来源的历史修成结构合法的消息序列。

    1. assistant 内容剥副作用标签、补悬空 <say>；
    2. 丢掉清理后变空的消息；
    3. 合并连续同角色消息——这正是历史被打断损坏后的形态（连着两条 user），
       部分 OpenAI 兼容端点会直接拒收；
    4. 丢掉开头的 assistant——system 之后必须由 user 起头。

    幂等：对已经干净的历史再跑一次不会有任何变化。
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
    """超出字符预算时从最老的开始丢，丢完再修一次结构。

    对应 MaiBot 的 `_trim_history_to_context_target`：裁切之后必须重新
    normalize，否则可能裁出一个 assistant 开头的历史。
    """

    if budget <= 0:
        return messages
    kept = list(messages)
    total = sum(len(m['content']) for m in kept)
    while kept and total > budget:
        total -= len(kept[0]['content'])
        kept.pop(0)
    return normalize_history(kept)
