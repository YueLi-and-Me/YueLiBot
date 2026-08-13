"""执行不进入业务服务的隔离模型重放。"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Dict, List, NamedTuple, Tuple

from src.core.llm_models.protocol import LlmProvider
from src.core.observe.events import broadcaster
from src.core.observe.store import EventStore
from src.core.prompts.registry import (
    CHAT_PROACTIVE_TEMPLATE_IDS,
    CHAT_SYSTEM_COMPONENTS,
    CHAT_SYSTEM_TEMPLATE_IDS,
    get_prompt,
    render_chat_system,
)


class ReplayDefinition(NamedTuple):
    """一类模型请求的重放路由和提示词模板定义。"""

    task: str
    template_ids: Tuple[str, ...]


_REPLAY_DEFINITIONS: Dict[str, ReplayDefinition] = {
    'chat.system': ReplayDefinition('chat', CHAT_SYSTEM_TEMPLATE_IDS),
    'chat.proactive': ReplayDefinition('chat', CHAT_PROACTIVE_TEMPLATE_IDS),
    'summary': ReplayDefinition('summary', ('summary',)),
    'schedule': ReplayDefinition('schedule', ('schedule',)),
    'expression.select': ReplayDefinition('expression', ('expression.select',)),
    'vision.glance': ReplayDefinition('vision', ('vision.glance',)),
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
    definition = _REPLAY_DEFINITIONS.get(prompt_id)
    if definition is None:
        raise ValueError(f'模型请求 {event["seq"]} 的 promptId 不支持重放：{prompt_id}')
    return definition.task


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
    params: Dict[str, Dict[str, str]] = {}
    if 'chat.system' in template_ids:
        render_template_ids = (*CHAT_SYSTEM_COMPONENTS, 'chat.system')
        if 'chat.proactive' in template_ids:
            render_template_ids = (*render_template_ids, 'chat.proactive')
    else:
        render_template_ids = template_ids
    for template_id in render_template_ids:
        values = render_params.get(template_id)
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in values.items()
        ):
            raise ValueError(f'事件 {event["seq"]} 缺少模板 {template_id} 的渲染参数')
        params[template_id] = dict(values)
    rendered: Dict[str, str] = {}
    if 'chat.system' in template_ids:
        rendered['chat.system'], _ = render_chat_system(
            params['chat.system'],
            {
                template_id: params[template_id]
                for template_id in CHAT_SYSTEM_COMPONENTS
            },
        )
        if 'chat.proactive' in template_ids:
            rendered['chat.proactive'] = get_prompt('chat.proactive').render(
                **params['chat.proactive']
            )
    else:
        template_id = template_ids[0]
        rendered[template_id] = get_prompt(template_id).render(**params[template_id])
    if 'chat.system' in rendered:
        parts = [rendered['chat.system']]
        if 'chat.proactive' in rendered:
            parts.append(rendered['chat.proactive'])
        return '\n\n'.join(parts)
    return rendered[template_ids[0]]


def _replace_prompt(
    messages: List[Dict[str, Any]],
    prompt: str,
    prompt_id: str,
) -> List[Dict[str, Any]]:
    """替换首条消息中的提示词正文，同时保留其余历史上下文。"""
    rebuilt = [dict(message) for message in messages]
    _prompt_text(rebuilt, prompt_id)
    content = rebuilt[0]['content']
    if isinstance(content, list):
        parts = [dict(part) if isinstance(part, dict) else part for part in content]
        text_index = next(
            index for index, part in enumerate(parts)
            if isinstance(part, dict) and part.get('type') == 'text'
        )
        parts[text_index]['text'] = prompt
        rebuilt[0]['content'] = parts
    else:
        rebuilt[0]['content'] = prompt
    return rebuilt


def _prompt_text(messages: List[Dict[str, Any]], prompt_id: str) -> str:
    """取得原模型请求中实际发送的提示词正文。"""
    expected_role = 'user' if prompt_id == 'vision.glance' else 'system'
    if messages[0]['role'] != expected_role:
        raise ValueError(
            f'提示词 {prompt_id} 的首条消息角色应为 {expected_role}，'
            f'实际为 {messages[0]["role"]}'
        )
    content = messages[0].get('content')
    if prompt_id == 'vision.glance' and not isinstance(content, list):
        raise ValueError('提示词 vision.glance 的首条消息 content 应为分段数组')
    if isinstance(content, list):
        text_part = next((
            part for part in content
            if isinstance(part, dict) and part.get('type') == 'text'
        ), None)
        if text_part is None or not isinstance(text_part.get('text'), str):
            raise ValueError('原模型请求的首条消息没有可替换的文本提示词')
        return text_part['text']
    if not isinstance(content, str):
        raise ValueError('原模型请求的首条消息没有字符串提示词正文')
    return content


def _rebuild_messages(event: Dict[str, Any]) -> tuple[List[Dict[str, Any]], str]:
    """用原始渲染参数和当前模板重新构造模型消息。"""
    original = _messages(event.get('messages'))
    prompt_id = str(event.get('promptId', ''))
    definition = _REPLAY_DEFINITIONS.get(prompt_id)
    if definition is None:
        raise ValueError(f'模型请求 {event["seq"]} 的 promptId 不支持重放：{prompt_id}')
    prompt = _render_current_prompt(event, definition.template_ids)
    rebuilt = _replace_prompt(original, prompt, prompt_id)
    return rebuilt, sha256(prompt.encode('utf-8')).hexdigest()[:8]


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
    prompt_id = str(source.get('promptId', ''))
    original_messages = _messages(source.get('messages'))
    original_prompt = _prompt_text(original_messages, prompt_id)
    messages, replay_hash = _rebuild_messages(source)
    original_hash = sha256(original_prompt.encode('utf-8')).hexdigest()[:8]
    template_hash = str(source.get('promptHash', ''))
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
            'templatePromptHash': template_hash,
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
        'templatePromptHash': template_hash,
        'requestSeq': request['seq'],
        'finalSeq': final['seq'],
    }
