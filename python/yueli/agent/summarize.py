"""
情节摘要生成。直接移植自 src/core/agent/summarize.ts。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import json
import re


@dataclass
class Episode:
    summary: str
    recall_cues: List[str]


_SYSTEM_PROMPT = """你替月璃整理那些快要淡出短期上下文的聊天。写出来的是她以后会想起的片段，不是会议纪要。

要求：
1. 只写对话里真的出现过的内容。分不清是谁说的就不写，不替任何人补动机。
2. summary 用月璃的第一人称写两到四句话。保留聊了什么、他当时的态度、我有什么具体感受、
   哪句话以后还接得上。宁可留下一个有辨识度的小细节，也不要写「我们围绕某话题进行了交流」这种空话。
3. 尽量留住一个只属于这次的具体锚点：他的一句原话、一个专有名词、一个数字或一个当时的小插曲。
   记忆是靠这种锚点被想起来的，泛化的概括等于没记。
4. 没有明确情绪就平实记录，不要凭空煽情；没有约定就不要制造「我们决定了」。
5. recall_cues 写 3 到 5 条自然语言检索线索。每条先想「以后在什么情境下会需要想起这段」，
   再写成一句包含话题、意图或关联事物的短句，而不是几个关键词。
6. 只输出一个 JSON 对象，不要用 Markdown 代码块，不要加解释。
   格式：{"summary":"...","recall_cues":["...","..."]}
"""


def _strip_tags(raw: str) -> str:
    raw = re.sub(r'<(memory|mood|think|thinking)\b[^>]*>[\s\S]*?</\1>', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'<(memory|mood)\b[^>]*/?>',  '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'</?say\b[^>]*>', '', raw, flags=re.IGNORECASE)
    return raw.strip()


def _render(messages: List[Dict[str, str]]) -> str:
    lines: List[str] = []
    for m in messages:
        text = _strip_tags(m['content']) if m.get('role') == 'assistant' else m.get('content', '')
        line = f"{'他' if m.get('role') == 'user' else '我'}：{text}"
        if len(line) > 3:
            lines.append(line)
    return '\n'.join(lines)


def parse_episode(raw: str) -> Optional[Episode]:
    text = re.sub(r'```(?:json)?', '', raw, flags=re.IGNORECASE).strip()
    start = text.find('{')
    end = text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        j = json.loads(text[start:end + 1])
        summary = j.get('summary', '')
        if not isinstance(summary, str) or not summary.strip():
            return None
        cues = [c.strip() for c in j.get('recall_cues', []) if isinstance(c, str) and c.strip()]
        return Episode(summary=summary.strip(), recall_cues=cues if cues else [summary.strip()])
    except Exception:
        return None


async def summarize(provider: Any, messages: List[Dict[str, str]]) -> Optional[Episode]:
    body = _render(messages)
    if len(body) < 40:
        return None
    raw = ''
    try:
        async for chunk in provider.stream(
            messages=[
                {'role': 'system', 'content': _SYSTEM_PROMPT},
                {'role': 'user', 'content': f'要整理的对话：\n{body}'},
            ],
            temperature=0.3,
        ):
            if chunk.get('text'):
                raw += chunk['text']
    except Exception:
        return None
    return parse_episode(raw)
