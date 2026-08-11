"""按可配置角色身份生成情节摘要。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import json
import re

from src.llm_models.protocol import LlmProvider
from src.observe import events as trace
from src.prompts.registry import get_prompt, prompt_metadata


@dataclass
class Episode:
    summary: str
    recall_cues: List[str]


def _system_prompt(character_name: str, character_personality: str) -> str:
    return get_prompt('summary').render(
        character_name=character_name,
        character_personality=character_personality,
    )


def _strip_tags(raw: str) -> str:
    raw = re.sub(r'<(memory|mood|think|thinking)\b[^>]*>[\s\S]*?</\1>', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'<(memory|mood)\b[^>]*/?>',  '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'</?say\b[^>]*>', '', raw, flags=re.IGNORECASE)
    return raw.strip()


def _render(messages: List[Dict[str, str]]) -> str:
    lines: List[str] = []
    for m in messages:
        text = _strip_tags(m['content']) if m.get('role') == 'assistant' else m.get('content', '')
        line = f"{'对方' if m.get('role') == 'user' else '我'}：{text}"
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


async def summarize(
    provider: LlmProvider,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: int | None,
    character_name: str,
    character_personality: str,
) -> Optional[Episode]:
    body = _render(messages)
    if len(body) < 40:
        return None
    raw = ''
    request_messages = [
        {
            'role': 'system',
            'content': _system_prompt(character_name, character_personality),
        },
        {'role': 'user', 'content': f'要整理的对话：\n{body}'},
    ]
    try:
        trace.emit(
            'llm_request',
            messages=request_messages,
            temperature=temperature,
            maxTokens=max_tokens,
            **prompt_metadata('summary', ('summary',)),
        )
        async for chunk in provider.stream(
            messages=request_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            if chunk.get('text'):
                raw += chunk['text']
    except Exception:
        return None
    return parse_episode(raw)
