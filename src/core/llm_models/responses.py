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
    """返回 extra_body 与受管字段的冲突说明，无冲突返回空串；加载期与请求时共用。

    :param extra_body: 调用方叠加的 extra_body 字典。
    :return: 逐条冲突说明以顿号连接的文本；无冲突时为空串。
    副作用：无。
    """
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
    """转换完整消息序列；未知内容块报 ValueError，受管字段冲突报模型错误。

    :param model: 模型标识。
    :param messages: 统一消息序列，content 可为字符串或内容块列表。
    :param temperature: 采样温度。
    :param max_tokens: 输出上限（含思考）；``None`` 表示不下发该字段。
    :param response_format: JSON 输出格式声明，可为 ``None``。
    :param tools: 工具声明列表，可为 ``None``；统一以 ``strict=false`` 下发。
    :param extra_body: 调用方叠加的额外字段，最后合并进请求体。
    :return: Responses 协议请求体。
    :raises LlmError: extra_body 覆盖受管字段或 store 非 false。
    :raises ValueError: 消息含本协议不支持的内容块。
    副作用：无。
    """
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
                    # Responses 的 image_url 为字符串，与 Chat 的嵌套对象不同；
                    # 沿用对象写法时接口返回 400。
                    image = block['image_url']
                    item = {'type': 'input_image', 'image_url': image['url']}
                    if 'detail' in image:
                        item['detail'] = image['detail']
                    converted.append(item)
                elif kind == 'video_url':
                    # video_url 同样必须为字符串；传入对象或改用 url 字段时，接口均判定
                    # video_url 为空，全模态请求因此无法携带视频。
                    converted.append({'type': 'input_video', 'video_url': block['video_url']['url']})
                else:
                    raise ValueError(f'Responses 不支持的内容块：{kind}')
            content = converted
        inputs.append({'role': message['role'], 'content': content})
    # Responses 缺省在服务端保存请求与回复以支持会话续接；为避免群聊与私聊原文
    # 留存在厂商侧，显式设置 store=false。
    body = {'model': model, 'input': inputs, 'stream': True, 'store': False, 'temperature': temperature}
    if max_tokens is not None:
        # max_output_tokens 包含思考 token；思考耗尽额度时正文为空，模型切换到本协议时
        # 需相应调高上限。
        body['max_output_tokens'] = max_tokens
    if response_format is not None:
        body['text'] = {'format': response_format}
    if tools:
        # Responses 缺省按 strict=true 规范化工具 schema，模型会为可选参数填入空值；
        # Chat 缺省为 false。显式设置 strict=false，保持可选参数可省略的语义。
        body['tools'] = [{'type': 'function', **tool['function'], 'strict': False} for tool in tools]
    body.update(extra_body)
    return body


def classify_error(code: str, message: str) -> str:
    """复用错误码分类，仅对接口实测的免费额度消息前缀作精确修正。

    :param code: 服务端返回的错误码。
    :param message: 服务端返回的错误消息。
    :return: 统一错误类别（如 ``billing``、``server``）。
    副作用：无。
    """
    # 百炼在 Responses 上将免费额度耗尽报告为 server_error，仅按错误码会误判为服务端
    # 故障；该消息前缀表示账务拒绝，归为 billing 可避免无效重试。
    if message.startswith('Free quota exhausted'):
        return 'billing'
    return _classify_code(code)


class ResponsesParser:
    """保存单次 Responses 流的终止状态、正文存在性与完整工具调用。"""

    def __init__(self) -> None:
        """创建未终止、无正文的空解析器。

        副作用：仅初始化进程内状态。
        """
        self.terminated = False
        self.has_text = False
        self._calls: List[Dict[str, str]] = []

    def parse(self, line: str) -> Dict | str | None:
        """解析 data 行；拒绝、错误及无正文截断抛出分类明确的 LlmError。

        :param line: SSE 流中的一行。
        :return: 文本增量 ``{'text': ...}``、思考增量 ``{'reasoning': ...}``；
            终止事件返回 ``'done'``，其余行返回 ``None``。
        :raises LlmError: data 行非法、模型返回错误信封、内容被拒或截断。
        副作用：更新解析器的终止与正文状态；完整工具调用条目入队待 :meth:`drain`。
        """
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
        # 部分错误以不含 type 的 {code, message} data 行返回；仅按事件名分派会丢失
        # 拒绝原因，并在流结束时误判为断流。
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
            # item.id 为条目编号，item.call_id 为调用编号，工具结果按调用编号对应；
            # 因此在条目完整到达时读取 call_id，不拼接参数增量。
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
        """取出并清空已完成的工具调用，调用方不接触参数分片。"""
        calls, self._calls = self._calls, []
        return calls
