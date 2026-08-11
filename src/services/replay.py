"""执行不进入业务服务的隔离模型重放。"""

from __future__ import annotations

from typing import Any, Dict, List

from src.llm_models.protocol import LlmProvider
from src.observe.events import broadcaster
from src.observe.store import EventStore
from src.prompts.registry import (
    CHAT_PROACTIVE_TEMPLATE_IDS,
    CHAT_SYSTEM_TEMPLATE_IDS,
    get_prompt,
    prompt_metadata,
)


_PROMPT_TEMPLATES = {
    'chat.system': CHAT_SYSTEM_TEMPLATE_IDS,
    'chat.proactive': CHAT_PROACTIVE_TEMPLATE_IDS,
    'summary': ('summary',),
    'schedule': ('schedule',),
    'expression.select': ('expression.select',),
    'vision.glance': ('vision.glance',),
}


def _messages(value: Any) -> List[Dict[str, Any]]:
    """严格解析账本中的 OpenAI 风格消息数组。"""
    if not isinstance(value, list) or not value:
        raise ValueError('原模型请求没有可重放的 messages')
    messages: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get('role'), str):
            raise ValueError('原模型请求的 messages 格式不合法')
        messages.append(dict(item))
    return messages


def _rebuild_messages(event: Dict[str, Any]) -> tuple[List[Dict[str, Any]], str]:
    """用当前模板约束和账本上下文快照重新构造模型消息。"""
    original = _messages(event.get('messages'))
    prompt_id = str(event.get('promptId', ''))
    template_ids = _PROMPT_TEMPLATES.get(prompt_id)
    if template_ids is None:
        raise ValueError(f'模型请求 {event["seq"]} 的 promptId 不支持重放：{prompt_id}')
    current_templates = '\n\n'.join(
        f'## {template_id}\n{get_prompt(template_id).text}'
        for template_id in template_ids
    )
    original_system = ''
    remaining = original
    if original[0].get('role') == 'system':
        original_system = str(original[0].get('content', ''))
        remaining = original[1:]
    system = '\n\n'.join([
        '# 当前生效模板',
        current_templates,
        '# 原请求上下文快照',
        original_system,
        '请在不查询或补充任何当前业务数据的前提下，依据以上模板与历史消息重新生成。',
    ])
    metadata = prompt_metadata(prompt_id, template_ids)
    return [{'role': 'system', 'content': system}, *remaining], metadata['promptHash']


async def replay_event(
    store: EventStore,
    provider: LlmProvider,
    seq: int,
) -> Dict[str, Any]:
    """隔离重放一条持久化 ``llm_request`` 并返回原新输出对照。"""
    source = store.event(seq)
    if source is None:
        raise LookupError(f'事件 {seq} 不存在')
    if source['kind'] != 'llm_request':
        raise ValueError(f'事件 {seq} 不是 llm_request')
    messages, replay_hash = _rebuild_messages(source)
    original_hash = str(source.get('promptHash', ''))
    request = store.append(
        'replay_request',
        str(source.get('stage', '')),
        source.get('streamId'),
        source.get('turnId'),
        {
            'sourceSeq': seq,
            'messages': messages,
            'temperature': source.get('temperature', 0.85),
            'maxTokens': source.get('maxTokens'),
            'originalPromptHash': original_hash,
            'promptHash': replay_hash,
        },
    )
    broadcaster.publish(request)
    output = ''
    async for chunk in provider.stream(
        messages=messages,
        temperature=float(source.get('temperature', 0.85)),
        max_tokens=source.get('maxTokens'),
    ):
        if chunk.get('text'):
            output += str(chunk['text'])
    final = store.append(
        'replay_final',
        str(source.get('stage', '')),
        source.get('streamId'),
        source.get('turnId'),
        {'sourceSeq': seq, 'text': output, 'promptHash': replay_hash},
    )
    broadcaster.publish(final)
    original = store.first_matching_after(
        seq,
        kind='llm_final',
        stream_id=source.get('streamId'),
        turn_id=source.get('turnId'),
    )
    return {
        'sourceSeq': seq,
        'originalOutput': str(original.get('text', '')) if original is not None else '',
        'replayOutput': output,
        'originalPromptHash': original_hash,
        'replayPromptHash': replay_hash,
        'requestSeq': request['seq'],
        'finalSeq': final['seq'],
    }
