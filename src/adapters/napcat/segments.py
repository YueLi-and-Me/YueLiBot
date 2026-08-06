"""OneBot v11 array 消息段与文本之间的纯转换。"""

from __future__ import annotations

from typing import Any, Dict, Literal, Mapping, Sequence


Segment = Mapping[str, Any]
ImageSourceKind = Literal['base64', 'file']

_IMAGE_SUBTYPES_THAT_ARE_NOT_EMOJI = frozenset({0, 4, 9})


def is_emoji_image(segment: Segment) -> bool:
    """判断 image 段是否为表情包；缺少 sub_type 时明确视为普通图片。"""
    if segment.get('type') != 'image':
        return False
    data = _segment_data(segment)
    subtype = data.get('sub_type')
    return subtype is not None and subtype not in _IMAGE_SUBTYPES_THAT_ARE_NOT_EMOJI


def segment_to_text(segment: Segment) -> str:
    """把单个消息段转成能交给对话模型理解的文本或占位描述。"""
    segment_type = _segment_type(segment)
    data = _segment_data(segment)

    if segment_type == 'text':
        text = data.get('text')
        if not isinstance(text, str):
            raise ValueError('text 消息段的 data.text 必须是字符串')
        return text
    if segment_type == 'at':
        qq = _required_value(data.get('qq'), 'at 消息段缺少 data.qq')
        return '@全体成员' if qq == 'all' else f'@{qq}'
    if segment_type == 'image':
        return '[表情包]' if is_emoji_image(segment) else '[图片]'

    placeholders = {
        'face': '[表情]',
        'record': '[语音]',
        'video': '[视频]',
        'file': '[文件]',
        'reply': '[引用消息]',
        'share': '[分享]',
        'location': '[位置]',
        'contact': '[联系人]',
        'json': '[JSON 消息]',
        'xml': '[XML 消息]',
        'forward': '[转发消息]',
    }
    return placeholders.get(segment_type, f'[非文本消息：{segment_type}]')


def message_to_text(segments: Sequence[Segment]) -> str:
    """按协议数组顺序拼接整条消息，非文本内容保留为占位描述。"""
    if not isinstance(segments, list):
        raise ValueError('message 必须是 array 格式的消息段列表')
    return ''.join(segment_to_text(segment) for segment in segments)


def mentions_user(segments: Sequence[Segment], user_id: str) -> bool:
    """判断消息数组是否包含 @ 指定 QQ 号或 @全体成员。"""
    target = _required_value(user_id, 'user_id 不能为空')
    if not isinstance(segments, list):
        raise ValueError('message 必须是 array 格式的消息段列表')
    for segment in segments:
        if segment.get('type') != 'at':
            continue
        qq = _segment_data(segment).get('qq')
        if qq == 'all' or _string_value(qq) == target:
            return True
    return False


def base64_image_segment(content: str) -> Dict[str, Any]:
    """生成带有强制 base64:// 前缀的出站图片段。"""
    return _image_segment(content, 'base64')


def file_image_segment(path: str) -> Dict[str, Any]:
    """生成带有强制 file:// 前缀的本地文件图片段。"""
    return _image_segment(path, 'file')


def _image_segment(source: str, source_kind: ImageSourceKind) -> Dict[str, Any]:
    value = _required_value(source, '图片来源不能为空')
    expected_prefix = f'{source_kind}://'
    other_kind = 'file' if source_kind == 'base64' else 'base64'
    other_prefix = f'{other_kind}://'
    if value.startswith(other_prefix):
        raise ValueError(f'{source_kind} 图片不能使用 {other_prefix} 前缀')
    if not value.startswith(expected_prefix):
        value = expected_prefix + value
    return {'type': 'image', 'data': {'file': value}}


def _segment_type(segment: Segment) -> str:
    segment_type = segment.get('type')
    if not isinstance(segment_type, str) or not segment_type.strip():
        raise ValueError('消息段缺少非空 type')
    return segment_type.strip()


def _segment_data(segment: Segment) -> Mapping[str, Any]:
    data = segment.get('data')
    if not isinstance(data, Mapping):
        raise ValueError('消息段缺少对象类型的 data')
    return data


def _required_value(value: Any, message: str) -> str:
    normalized = _string_value(value)
    if not normalized:
        raise ValueError(message)
    return normalized


def _string_value(value: Any) -> str:
    if value is None:
        return ''
    return str(value).strip()
