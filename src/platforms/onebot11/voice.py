"""把协议端原生语音转文字结果并入入站正文。

运行器完成访问名单过滤后调用本模块；只使用当前适配器声明的转写能力。
按消息段位置重建正文，避免用户手写的「[语音]」或引用摘要被误替换。
识别失败保留明确的失败状态并记录原因，不推测内容或改走其他服务。
"""

from dataclasses import replace
from typing import Any, List, Mapping

import asyncio

from src.core.logging.logger import get_logger

from .events import QqInboundEvent
from .segments import authored_text, message_to_text
from .transport import ActionError, OneBot11Transport


logger = get_logger(__name__)


async def transcribe_inbound(
    transport: OneBot11Transport,
    event: QqInboundEvent,
    segments: List[Mapping[str, Any]],
    *,
    supported: bool,
    mention_names: Mapping[str, str],
    quote_previews: Mapping[str, str],
) -> QqInboundEvent:
    """转写一条含语音的消息；保留原位置、文本、提及、引用与媒体来源。

    原生接口按消息 ID 返回一份转写，无法区分同一消息的多个语音段，因此拒绝将
    一份结果重复分配给多个段。协议错误只影响本条语音；取消与连接断开继续传播。
    """
    record_count = sum(part.get('type') == 'record' for part in segments)
    if not record_count:
        return event
    transcript = ''
    status = '当前适配器不支持转写'
    if supported:
        try:
            if record_count != 1:
                raise ValueError('按消息编号转写的接口不支持同一消息包含多个语音段')
            response = await transport.call_action(
                'get_record_text', {'message_id': event.external_message_id},
            )
            data = response.get('data')
            if not isinstance(data, Mapping) or not isinstance(data.get('text'), str):
                raise ValueError('语音转文字响应缺少 data.text 字符串')
            transcript = data['text'].strip()
            if not transcript:
                raise ValueError('语音转文字返回空文本')
            status = transcript
        except (ActionError, asyncio.TimeoutError, ValueError) as exc:
            status = '转写失败，内容未知'
            logger.error('QQ 语音转写失败', messageId=event.external_message_id, error=str(exc))
    else:
        logger.warning('QQ 语音未转写，适配器未声明能力', messageId=event.external_message_id)
    rendered_segments = [
        {'type': 'text', 'data': {'text': f'[语音：{status}]'}}
        if part.get('type') == 'record' else part
        for part in segments
    ]
    spoken_segments = [
        {'type': 'text', 'data': {'text': transcript}}
        if part.get('type') == 'record' else part
        for part in segments
    ]
    return replace(
        event,
        text=message_to_text(rendered_segments, mention_names, quote_previews),
        # 语音原话与键盘输入一样属于用户本人，用于叫名字与补发内容判定。
        authored_text=authored_text(spoken_segments),
    )
