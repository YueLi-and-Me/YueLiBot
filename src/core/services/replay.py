"""执行不进入业务服务的隔离模型重放。"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Dict, List

from src.core.llm_models.protocol import LlmProvider
from src.core.observe.events import broadcaster
from src.core.observe.store import EventStore
from src.core.prompts.registry import (
    CHAT_PROACTIVE_TEMPLATE_IDS,
    CHAT_SYSTEM_TEMPLATE_IDS,
    get_prompt,
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


def replay_task(event: Dict[str, Any]) -> str:
    """返回一条可重放事件对应的模型任务名。"""
    prompt_id = str(event.get('promptId', ''))
    tasks = {
        'chat.system': 'chat',
        'chat.proactive': 'chat',
        'summary': 'summary',
        'schedule': 'schedule',
        'expression.select': 'expression',
        'vision.glance': 'vision',
    }
    task = tasks.get(prompt_id)
    if task is None:
        raise ValueError(f'模型请求 {event["seq"]} 的 promptId 不支持重放：{prompt_id}')
    return task


def replay_task_for_seq(store: EventStore, seq: int) -> str:
    """读取账本事件并返回重放任务名。"""
    source = store.event(seq)
    if source is None:
        raise LookupError(f'事件 {seq} 不存在')
    if source['kind'] != 'llm_request':
        raise ValueError(f'事件 {seq} 不是 llm_request')
    return replay_task(source)


def _render_current_prompt(event: Dict[str, Any], template_ids: tuple[str, ...]) -> str:
    """使用账本中的原始渲染参数渲染当前模板。"""
    render_params = event.get('renderParams')
    if not isinstance(render_params, dict):
        raise ValueError(f'事件 {event["seq"]} 早于渲染参数落库，无法准确重放')
    rendered: Dict[str, str] = {}
    for template_id in template_ids:
        values = render_params.get(template_id)
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in values.items()
        ):
            raise ValueError(f'事件 {event["seq"]} 缺少模板 {template_id} 的渲染参数')
        rendered[template_id] = get_prompt(template_id).render(**values)
    if 'chat.system' in rendered:
        parts = [rendered['chat.system']]
        if 'chat.proactive' in rendered:
            parts.append(rendered['chat.proactive'])
        return '\n\n'.join(parts)
    return rendered[template_ids[0]]


def _replace_prompt(messages: List[Dict[str, Any]], prompt: str) -> List[Dict[str, Any]]:
    """替换首条消息中的提示词正文，同时保留其余历史上下文。"""
    rebuilt = [dict(message) for message in messages]
    content = rebuilt[0].get('content')
    if isinstance(content, list):
        parts = [dict(part) if isinstance(part, dict) else part for part in content]
        text_index = next((
            index for index, part in enumerate(parts)
            if isinstance(part, dict) and part.get('type') == 'text'
        ), None)
        if text_index is None:
            raise ValueError('原模型请求的首条消息没有可替换的文本提示词')
        parts[text_index]['text'] = prompt
        rebuilt[0]['content'] = parts
    else:
        rebuilt[0]['content'] = prompt
    return rebuilt


def _rebuild_messages(event: Dict[str, Any]) -> tuple[List[Dict[str, Any]], str]:
    """用原始渲染参数和当前模板重新构造模型消息。"""
    original = _messages(event.get('messages'))
    prompt_id = str(event.get('promptId', ''))
    template_ids = _PROMPT_TEMPLATES.get(prompt_id)
    if template_ids is None:
        raise ValueError(f'模型请求 {event["seq"]} 的 promptId 不支持重放：{prompt_id}')
    prompt = _render_current_prompt(event, template_ids)
    return _replace_prompt(original, prompt), sha256(prompt.encode('utf-8')).hexdigest()[:8]


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
