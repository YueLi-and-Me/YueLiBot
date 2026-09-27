"""将调用方的 Chat 消息转换成 Responses 请求，并把事件流还原为统一增量。

由 openai provider 选择本线格式；传输、重试与正文守卫仍由 provider 负责。
每次请求持有独立解析器，工具调用只在终止事件后一次性交付。
"""

from typing import Any, Dict, List
import json

from .openai import LlmError, _classify_code, _extract_status_error


# 由本协议管理、不允许 extra_body 覆盖的请求字段；store 另需恒为 false。
RESERVED_EXTRA_BODY_KEYS = frozenset({'model', 'input', 'stream', 'previous_response_id'})


def extra_body_conflict(extra_body: Dict) -> str:
    """返回 extra_body 与受管字段的冲突说明，无冲突返回空串；加载期与请求时共用。"""
    problems = []
    reserved = sorted(RESERVED_EXTRA_BODY_KEYS & extra_body.keys())
    if reserved:
        problems.append(f'不得覆盖 {reserved}')
    if 'store' in extra_body and extra_body['store'] is not False:
        problems.append('store 必须为 false')
    return '，'.join(problems)


def build_body(
    model: str, messages: List[Dict], temperature: float, max_tokens: int | None,
    response_format: Dict | None, tools: List[Dict] | None, extra_body: Dict,
) -> Dict[str, Any]:
    """转换完整消息序列；未知内容块报 ValueError，受管字段冲突报模型错误。"""
    conflict = extra_body_conflict(extra_body)
    if conflict:
        raise LlmError('model', f'Responses extra_body {conflict}')
    inputs = []
    for message in messages:
        content = message['content']
        if isinstance(content, list):
            converted = []
            for block in content:
                kind = block['type']
                if kind == 'text':
                    converted.append({'type': 'output_text' if message['role'] == 'assistant' else 'input_text', 'text': block['text']})
                elif kind == 'image_url':
                    # 对象写法会被接口拒绝：Responses 的 image_url 是字符串，
                    # 与 Chat 嵌套对象不同；保留对象会使图片请求直接返回 400。
                    image = block['image_url']
                    item = {'type': 'input_image', 'image_url': image['url']}
                    if 'detail' in image:
                        item['detail'] = image['detail']
                    converted.append(item)
                elif kind == 'video_url':
                    # video_url 同样必须为字符串；对象或改名 url 都被判为空，
                    # 因此只展开值，不改变字段名，否则全模态请求无法使用视频。
                    converted.append({'type': 'input_video', 'video_url': block['video_url']['url']})
                else:
                    raise ValueError(f'Responses 不支持的内容块：{kind}')
            content = converted
        inputs.append({'role': message['role'], 'content': content})
    # 缺省 store 会在服务端保存请求与回复；Responses 默认用于会话续接，
    # 群聊与私聊原文因此可能滞留厂商侧，必须显式关闭存储。
    body = {'model': model, 'input': inputs, 'stream': True, 'store': False, 'temperature': temperature}
    if max_tokens is not None:
        # 此上限包含思考；思考耗尽预算会留下空正文，切换协议时必须留足余量。
        body['max_output_tokens'] = max_tokens
    if response_format is not None:
        body['text'] = {'format': response_format}
    if tools:
        # Responses 缺省按 strict=true 规范化 schema，模型会给可选参数填空值；
        # Chat 缺省为 false，显式关闭才不会静默改变工具参数的省略语义。
        body['tools'] = [{'type': 'function', **tool['function'], 'strict': False} for tool in tools]
    body.update(extra_body)
    return body


def classify_error(code: str, message: str) -> str:
    """复用错误码分类，仅对接口实测的免费额度消息前缀作精确修正。"""
    # 免费额度耗尽被包装为 server_error，按码会误判；这个精确前缀表示
    # 账务拒绝，归为 billing 才不会进行无意义的请求重试。
    if message.startswith('Free quota exhausted'):
        return 'billing'
    return _classify_code(code)


class ResponsesParser:
    """保存单次 Responses 流的终止状态、正文存在性与完整工具调用。"""

    def __init__(self) -> None:
        self.terminated = False
        self.has_text = False
        self._calls: List[Dict[str, str]] = []

    def parse(self, line: str) -> Dict | str | None:
        """解析 data 行；拒绝、错误及无正文截断抛出分类明确的 LlmError。"""
        line = line.strip()
        if not line.startswith('data:'):
            return None
        payload = line[5:].strip()
        if payload == '[DONE]':
            return None
        try:
            event = json.loads(payload)
        except ValueError as exc:
            raise LlmError('format', 'Responses data 行不是合法 JSON') from exc
        kind = event.get('type')
        # 错误有时没有 type，只有 code/message；仅按事件名分派会丢失真实
        # 拒绝原因并误报断流，所以必须同时识别这个错误信封。
        if kind in ('response.failed', 'error') or (kind is None and ('code' in event or 'message' in event)):
            self.terminated = True
            source = event['response'] if kind == 'response.failed' else event
            code, message = _extract_status_error(source)
            raise LlmError(classify_error(code, message), f'模型返回错误：{code} {message}')
        if kind == 'response.output_text.delta':
            self.has_text = self.has_text or bool(event['delta'])
            return {'text': event['delta']}
        if kind in ('response.reasoning_text.delta', 'response.reasoning_summary_text.delta'):
            return {'reasoning': event['delta']}
        if kind == 'response.output_item.done' and event['item']['type'] == 'function_call':
            item = event['item']
            # id 是消息条目号，call_id 才是调用号；取错会使工具结果与调用
            # 无法对应，因此只在完整条目到达时读取 call_id，不拼接 delta。
            self._calls.append({'id': item['call_id'], 'name': item['name'], 'arguments': item['arguments']})
        if kind == 'response.refusal.delta':
            raise LlmError('blocked', f"模型拒绝请求：{event.get('delta', '')}")
        if kind in ('response.completed', 'response.incomplete'):
            self.terminated = True
            if kind == 'response.incomplete':
                reason = event['response'].get('incomplete_details', {}).get('reason')
                if reason == 'content_filter':
                    raise LlmError('blocked', '模型输出被内容策略截断')
                if reason == 'max_output_tokens' and not self.has_text:
                    raise LlmError('format', '输出上限被思考耗尽，没有产出正文：请调大模型级 max_tokens 或降低思考档位')
            return 'done'
        return None

    def finish(self) -> None:
        """物理 EOF 不代表协议完成；缺少终止事件按网络断流处理。"""
        if not self.terminated:
            raise LlmError('network', 'Responses 流在完成事件前断开')

    def drain(self) -> List[Dict[str, str]]:
        """一次取走已完成的调用，避免向调用方暴露参数分片。"""
        calls, self._calls = self._calls, []
        return calls
