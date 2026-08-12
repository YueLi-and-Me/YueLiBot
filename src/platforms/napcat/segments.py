"""在 OneBot v11 数组消息段与主体可理解的文本之间执行纯转换。

本模块识别文本、提及、图片和其他非文本消息段，生成模型可读的占位描述，
同时为 QQ 出站图片构造带明确来源协议前缀的消息段；函数不执行文件读取或网络 I/O。
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Mapping, Sequence


Segment = Mapping[str, Any]
ImageSourceKind = Literal['base64', 'file']

_IMAGE_SUBTYPES_THAT_ARE_NOT_EMOJI = frozenset({0, 4, 9})


def is_emoji_image(segment: Segment) -> bool:
    """判断消息段是否为带明确表情子类型的图片消息。

    Args:
        segment: OneBot 消息段映射。

    Returns:
        类型为 ``image`` 且 ``sub_type`` 存在、同时不属于普通图片子类型集合时返回
        ``True``；缺少子类型时返回 ``False``。

    Raises:
        ValueError: 图片段缺少对象型 ``data`` 字段。
    """
    if segment.get('type') != 'image':
        return False
    data = _segment_data(segment)
    subtype = data.get('sub_type')
    return subtype is not None and subtype not in _IMAGE_SUBTYPES_THAT_ARE_NOT_EMOJI


def segment_to_text(
    segment: Segment,
    mention_names: Mapping[str, str] | None = None,
) -> str:
    """将单个 OneBot 消息段转换为模型可理解的文本或稳定占位描述。

    Args:
        segment: OneBot 消息段映射，必须包含非空 ``type`` 和对象型 ``data``。
        mention_names: 可选 QQ 号到显示名的映射，用于渲染提及文本。

    Returns:
        文本段原文、提及文本、图片或其他非文本消息的中文占位描述。

    Raises:
        ValueError: 消息段缺少必要字段，或文本段的 ``data.text`` 不是字符串。

    Side Effects:
        仅读取输入映射，不读取文件、不访问网络，也不修改输入数据。
    """
    segment_type = _segment_type(segment)
    data = _segment_data(segment)

    # 文本和提及保留语义内容；图片及协议扩展转换为稳定占位符，避免把原始结构直接交给模型。
    if segment_type == 'text':
        text = data.get('text')
        if not isinstance(text, str):
            raise ValueError('text 消息段的 data.text 必须是字符串')
        return text
    if segment_type == 'at':
        qq = _required_value(data.get('qq'), 'at 消息段缺少 data.qq')
        if qq == 'all':
            return '@全体成员'
        if mention_names is not None and qq in mention_names:
            name = _required_value(mention_names[qq], f'QQ {qq} 的显示名不能为空')
            return f'@{name}'
        return f'@{qq}'
    if segment_type == 'image':
        return '[表情包]' if is_emoji_image(segment) else '[图片]'

    # 未知段类型也保留类型名称，便于模型知道消息存在而不臆造具体内容。
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


def message_to_text(
    segments: Sequence[Segment],
    mention_names: Mapping[str, str] | None = None,
) -> str:
    """按协议顺序拼接消息段，并为非文本内容保留稳定占位描述。

    Args:
        segments: OneBot 消息段列表；必须为列表而不是其他序列类型。
        mention_names: 可选 QQ 号到显示名的映射，传递给单段转换逻辑。

    Returns:
        由各消息段转换结果无分隔符拼接而成的完整文本。

    Raises:
        ValueError: ``segments`` 不是列表，或任一消息段结构不合法。
    """
    if not isinstance(segments, list):
        raise ValueError('message 必须是 array 格式的消息段列表')
    return ''.join(segment_to_text(segment, mention_names) for segment in segments)


def mentions_user(segments: Sequence[Segment], user_id: str) -> bool:
    """判断消息段列表是否提及指定 QQ 号或全体成员。

    Args:
        segments: OneBot 消息段列表；必须为列表。
        user_id: 待匹配的 QQ 号，必须为非空标识字符串。

    Returns:
        存在 ``@`` 指定用户或 ``@all`` 段时返回 ``True``，否则返回 ``False``。

    Raises:
        ValueError: 用户号为空、消息段列表类型错误或某段缺少合法 ``data``。
    """
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
    """构造使用 ``base64://`` 来源协议的 OneBot 出站图片段。

    Args:
        content: Base64 图片内容，可带或不带 ``base64://`` 前缀；不能为空。

    Returns:
        包含 ``type=image`` 和规范化 ``data.file`` 的新字典。

    Raises:
        ValueError: 内容为空，或错误使用 ``file://`` 前缀。
    """
    return _image_segment(content, 'base64')


def file_image_segment(path: str) -> Dict[str, Any]:
    """构造使用 ``file://`` 来源协议的 OneBot 本地文件图片段。

    Args:
        path: 本地图片路径，可带或不带 ``file://`` 前缀；不能为空。

    Returns:
        包含 ``type=image`` 和规范化 ``data.file`` 的新字典。

    Raises:
        ValueError: 路径为空，或错误使用 ``base64://`` 前缀。
    """
    return _image_segment(path, 'file')


def _image_segment(source: str, source_kind: ImageSourceKind) -> Dict[str, Any]:
    """构造带来源协议前缀的 OneBot 图片段。

    :param source: 图片内容或本地路径；可带也可不带对应的 `base64://` 或 `file://` 前缀。
    :param source_kind: 来源类型，只能为 `base64` 或 `file`。
    :return: 形如 `{'type': 'image', 'data': {'file': '...'}}` 的新字典。
    :raises ValueError: 来源为空，或错误地使用了另一种来源前缀。
    :side_effects: 不读取文件、不编码内容，也不修改输入字符串。
    """
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
    """读取并规范化消息段类型。

    :param segment: OneBot 消息段映射。
    :return: 去除首尾空白的 `type` 字段。
    :raises ValueError: `type` 缺失、不是字符串或为空白。
    :side_effects: 不修改消息段。
    """
    segment_type = segment.get('type')
    if not isinstance(segment_type, str) or not segment_type.strip():
        raise ValueError('消息段缺少非空 type')
    return segment_type.strip()


def _segment_data(segment: Segment) -> Mapping[str, Any]:
    """读取消息段的对象型 `data` 字段。

    :param segment: OneBot 消息段映射。
    :return: 原始 `data` 映射，不复制其内容。
    :raises ValueError: `data` 缺失或不是映射。
    :side_effects: 不修改消息段。
    """
    data = segment.get('data')
    if not isinstance(data, Mapping):
        raise ValueError('消息段缺少对象类型的 data')
    return data


def _required_value(value: Any, message: str) -> str:
    """把可选字段转换为非空字符串并在缺失时抛出指定错误。

    :param value: 原始字段值。
    :param message: 字段为空时使用的中文错误信息。
    :return: 去除首尾空白后的字符串。
    :raises ValueError: 转换结果为空。
    :side_effects: 不修改输入值。
    """
    normalized = _string_value(value)
    if not normalized:
        raise ValueError(message)
    return normalized


def _string_value(value: Any) -> str:
    """将协议值转换为空安全的字符串。

    :param value: 任意值；`None` 表示缺失。
    :return: `None` 对应空字符串，否则返回去除首尾空白的字符串表示。
    :side_effects: 不执行 I/O。
    """
    if value is None:
        return ''
    return str(value).strip()
