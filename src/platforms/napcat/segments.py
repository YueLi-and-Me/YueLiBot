"""在 OneBot v11 数组消息段与主体可理解的文本之间执行纯转换。

本模块识别文本、提及、引用、图片和其他非文本消息段，生成模型可读的占位描述，
同时为 QQ 出站文本、图片和引用构造带明确来源协议前缀的消息段；函数不执行文件
读取或网络 I/O。

提及显示名与引用原文都不在消息段里，需由 `runner` 先向协议端解析后作为映射传入
（``mention_names`` / ``quote_previews``）；``mentioned_user_ids`` 和
``quoted_message_ids`` 供 `runner` 得知本条消息需要解析哪些对象。
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Mapping, Sequence


Segment = Mapping[str, Any]
ImageSourceKind = Literal['base64', 'file']

_IMAGE_SUBTYPES_THAT_ARE_NOT_EMOJI = frozenset({0, 4, 9})


def is_emoji_image(segment: Segment) -> bool:
    """判断消息段是否为带明确表情子类型的图片消息。

    :param segment: OneBot 消息段映射。

    :return: 类型为 ``image`` 且 ``sub_type`` 存在、同时不属于普通图片子类型集合时返回
        ``True``；缺少子类型时返回 ``False``。

    :raises ValueError: 图片段缺少对象型 ``data`` 字段。
    """
    if segment.get('type') != 'image':
        return False
    data = _segment_data(segment)
    subtype = _image_sub_type(data.get('sub_type'))
    return subtype is not None and subtype not in _IMAGE_SUBTYPES_THAT_ARE_NOT_EMOJI


def segment_to_text(
    segment: Segment,
    mention_names: Mapping[str, str] | None = None,
    quote_previews: Mapping[str, str] | None = None,
) -> str:
    """将单个 OneBot 消息段转换为模型可理解的文本或稳定占位描述。

    :param segment: OneBot 消息段映射，必须包含非空 ``type`` 和对象型 ``data``。
    :param mention_names: 可选 QQ 号到显示名的映射，用于渲染提及文本。
    :param quote_previews: 可选的被引用消息 ID 到摘要文本的映射；命中时 ``reply``
        段渲染为该摘要，未命中时退回不含内容的占位符。

    :return: 文本段原文、提及文本、图片或其他非文本消息的中文占位描述。

    :raises ValueError: 消息段缺少必要字段，或文本段的 ``data.text`` 不是字符串。

    副作用：
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
    if segment_type == 'reply':
        # 只有占位符时模型无从判断被引用的是哪句话，只能顺着当前这条硬猜，
        # 群里连着几个「？」的引用尤其容易答非所问；摘要由运行器查协议端补齐。
        quoted_id = _string_value(data.get('id'))
        preview = (quote_previews or {}).get(quoted_id, '')
        return f'[{preview}]' if preview else '[引用消息]'

    # 未知段类型也保留类型名称，便于模型知道消息存在而不臆造具体内容。
    placeholders = {
        'face': '[表情]',
        'record': '[语音]',
        'video': '[视频]',
        'file': '[文件]',
        'share': '[分享]',
        'location': '[位置]',
        'contact': '[联系人]',
        'json': '[JSON 消息]',
        'xml': '[XML 消息]',
        'forward': '[转发消息]',
    }
    return placeholders.get(segment_type, f'[非文本消息：{segment_type}]')


def image_source_urls(segments: Sequence[Segment]) -> tuple[str, ...]:
    """提取普通图片段的下载来源，顺序与正文中的 ``[图片]`` 占位符一致。

    :param segments: OneBot 消息段列表；必须为列表。
    :return: 普通图片的 ``data.url`` 或 ``data.file`` 字符串；缺失来源的图片
        用空字符串占位，保证后续描述与占位符顺序对齐。表情包不进入返回值。
    :raises ValueError: ``segments`` 不是列表，或图片段结构不合法。
    """
    if not isinstance(segments, list):
        raise ValueError('message 必须是 array 格式的消息段列表')
    sources: list[str] = []
    for segment in segments:
        if segment.get('type') != 'image':
            continue
        if is_emoji_image(segment):
            continue
        data = _segment_data(segment)
        sources.append(_string_value(data.get('url')) or _string_value(data.get('file')))
    return tuple(sources)


def emoji_source_urls(segments: Sequence[Segment]) -> tuple[str, ...]:
    """提取表情包图片来源，顺序与正文 ``[表情包]`` 占位符一致。

    :param segments: OneBot 消息段列表；必须为列表。
    :return: 表情包的 ``data.url`` 或 ``data.file``；缺失来源时保留空字符串。
    :raises ValueError: 消息段列表或图片段结构不合法。
    """

    if not isinstance(segments, list):
        raise ValueError('message 必须是 array 格式的消息段列表')
    sources: list[str] = []
    for segment in segments:
        if segment.get('type') != 'image' or not is_emoji_image(segment):
            continue
        data = _segment_data(segment)
        sources.append(_string_value(data.get('url')) or _string_value(data.get('file')))
    return tuple(sources)


def emoji_sub_types(segments: Sequence[Segment]) -> tuple[int, ...]:
    """提取表情包子类型，顺序与表情包来源和正文占位符一致。

    :param segments: OneBot 消息段列表；必须为列表。
    :return: 每个表情包图片段规范化后的整数 ``data.sub_type``。
    :raises ValueError: 消息段列表、图片段结构或 ``sub_type`` 不合法。
    """

    if not isinstance(segments, list):
        raise ValueError('message 必须是 array 格式的消息段列表')
    sub_types: list[int] = []
    for segment in segments:
        if segment.get('type') != 'image' or not is_emoji_image(segment):
            continue
        sub_type = _image_sub_type(_segment_data(segment).get('sub_type'))
        if sub_type is None:
            raise ValueError('表情包图片段缺少 data.sub_type')
        sub_types.append(sub_type)
    return tuple(sub_types)


def message_to_text(
    segments: Sequence[Segment],
    mention_names: Mapping[str, str] | None = None,
    quote_previews: Mapping[str, str] | None = None,
) -> str:
    """按协议顺序拼接消息段，并为非文本内容保留稳定占位描述。

    QQ 客户端在引用回复时会自动在 ``reply`` 段后补一个指向被引用者的 ``at`` 段，
    该提及在客户端界面上并不可见。引用摘要已经点名被引用者，保留这个 ``at`` 只会
    让正文出现「[回复 甲：…]@甲 内容」这类重复称呼，因此紧跟 ``reply`` 的 ``at``
    统一丢弃；用户手动补的 ``@`` 不会紧贴引用段，不受影响。

    :param segments: OneBot 消息段列表；必须为列表而不是其他序列类型。
    :param mention_names: 可选 QQ 号到显示名的映射，传递给单段转换逻辑。
    :param quote_previews: 可选的被引用消息 ID 到摘要文本的映射，传递给单段转换逻辑。

    :return: 由各消息段转换结果无分隔符拼接而成的完整文本。

    :raises ValueError: ``segments`` 不是列表，或任一消息段结构不合法。
    """
    if not isinstance(segments, list):
        raise ValueError('message 必须是 array 格式的消息段列表')
    parts: list[str] = []
    previous_type = ''
    for segment in segments:
        segment_type = _segment_type(segment)
        if segment_type == 'at' and previous_type == 'reply':
            previous_type = segment_type
            continue
        parts.append(segment_to_text(segment, mention_names, quote_previews))
        previous_type = segment_type
    return ''.join(parts)


def mentioned_user_ids(segments: Sequence[Segment]) -> tuple[str, ...]:
    """提取正文里真正会渲染出来的被提及 QQ 号，供运行器解析显示名。

    过滤口径必须与 :func:`message_to_text` 保持一致：紧跟 ``reply`` 的 ``at`` 由
    客户端自动补入、渲染时已被丢弃，若在此返回会让运行器为一个不会出现在正文里的
    名字白查一次协议端。

    :param segments: OneBot 消息段列表；必须为列表。
    :return: 去重后的被提及 QQ 号，保持出现顺序；``@全体成员`` 不含具体号码，不返回。
    :raises ValueError: ``segments`` 不是列表，或某个消息段缺少合法 ``data``。
    """
    if not isinstance(segments, list):
        raise ValueError('message 必须是 array 格式的消息段列表')
    user_ids: list[str] = []
    previous_type = ''
    for segment in segments:
        segment_type = _segment_type(segment)
        if segment_type != 'at' or previous_type == 'reply':
            previous_type = segment_type
            continue
        previous_type = segment_type
        qq = _string_value(_segment_data(segment).get('qq'))
        if not qq or qq == 'all' or qq in user_ids:
            continue
        user_ids.append(qq)
    return tuple(user_ids)


def quoted_message_ids(segments: Sequence[Segment]) -> tuple[str, ...]:
    """提取消息中被引用的消息 ID，供运行器向协议端还原原文。

    :param segments: OneBot 消息段列表；必须为列表。
    :return: 去重后的被引用消息 ID，保持出现顺序；无引用时返回空元组。
    :raises ValueError: ``segments`` 不是列表，或某个消息段缺少合法 ``data``。
    """
    if not isinstance(segments, list):
        raise ValueError('message 必须是 array 格式的消息段列表')
    message_ids: list[str] = []
    for segment in segments:
        if segment.get('type') != 'reply':
            continue
        quoted_id = _string_value(_segment_data(segment).get('id'))
        if not quoted_id or quoted_id in message_ids:
            continue
        message_ids.append(quoted_id)
    return tuple(message_ids)


def mentions_user(segments: Sequence[Segment], user_id: str) -> bool:
    """判断消息段列表是否提及指定 QQ 号或全体成员。

    :param segments: OneBot 消息段列表；必须为列表。
    :param user_id: 待匹配的 QQ 号，必须为非空标识字符串。

    :return: 存在 ``@`` 指定用户或 ``@all`` 段时返回 ``True``，否则返回 ``False``。

    :raises ValueError: 用户号为空、消息段列表类型错误或某段缺少合法 ``data``。
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

    :param content: Base64 图片内容，可带或不带 ``base64://`` 前缀；不能为空。

    :return: 包含 ``type=image`` 和规范化 ``data.file`` 的新字典。

    :raises ValueError: 内容为空，或错误使用 ``file://`` 前缀。
    """
    return _image_segment(content, 'base64')


def file_image_segment(path: str, sub_type: int | None = None) -> Dict[str, Any]:
    """构造使用 ``file://`` 来源协议的 OneBot 本地文件图片段。

    :param path: 本地图片路径，可带或不带 ``file://`` 前缀；不能为空。

    :param sub_type: 可选 OneBot 图片子类型；表情包发送时必须显式提供。

    :return: 包含 ``type=image``、规范化 ``data.file`` 和可选 ``data.sub_type``
        的新字典。

    :raises ValueError: 路径为空，或错误使用 ``base64://`` 前缀。
    """
    return _image_segment(path, 'file', sub_type=sub_type)


def outbound_message_segments(
    text_segments: Sequence[str],
    emoji_refs: Sequence[str],
    emoji_sub_types: Sequence[int],
) -> list[Dict[str, Any]]:
    """按“文本在前、表情包在后”组装 OneBot 消息段序列。

    每条文本各自成段，不再拼接成一段：``outbound_message_batches`` 会把每个段
    发成一条独立消息，拼接会让整轮回复挤成一个气泡。历史上这里做 ``''.join``，
    结果是模型分好的 ``<say>`` 在最后一公里被还原成一大段，且连换行都没有。

    :param text_segments: 已按打字习惯切分的气泡文本，顺序即发送顺序。
    :param emoji_refs: 已通过启动哈希校验的本地 ``file://`` 图片引用。
    :param emoji_sub_types: 与引用逐项对齐的 OneBot 表情包子类型。
    :return: 与输入顺序一致的消息段序列，文字在前、表情包在后。
    :raises ValueError: 文本和表情包同时为空、引用与子类型数量不一致，或字段不合法。
    """

    texts = [segment for segment in text_segments if segment]
    if not texts and not emoji_refs:
        raise ValueError('QQ 出站消息必须包含文本或表情包')
    if len(emoji_refs) != len(emoji_sub_types):
        raise ValueError('QQ 出站表情包引用与 sub_type 数量必须一致')
    normalized_sub_types = tuple(_required_emoji_sub_type(value) for value in emoji_sub_types)
    return [
        *[{'type': 'text', 'data': {'text': text}} for text in texts],
        *[
            file_image_segment(reference, sub_type)
            for reference, sub_type in zip(
                emoji_refs,
                normalized_sub_types,
                strict=True,
            )
        ],
    ]


def reply_segment(message_id: str) -> Dict[str, Any]:
    """构造引用回复用的 OneBot ``reply`` 段。

    :param message_id: 被引用消息的平台编号，不能为空。
    :return: 形如 ``{'type': 'reply', 'data': {'id': '123'}}`` 的新字典。
    :raises ValueError: 消息编号为空。
    """
    return {'type': 'reply', 'data': {'id': _required_value(message_id, '引用消息编号不能为空')}}


def outbound_message_batches(
    text_segments: Sequence[str],
    emoji_refs: Sequence[str],
    emoji_sub_types: Sequence[int],
    quote_message_id: str = '',
) -> list[list[Dict[str, Any]]]:
    """把每条文字和每张表情包拆成独立的 OneBot 消息段数组。

    先完整校验全部字段，再返回“每条文字各一条、每张表情包各一条”的发送批次，
    避免文字已经发出后才发现表情包元数据不一致。独立 action 会让 QQ 为每条文字和
    表情包分别创建消息气泡，这正是她的分句在聊天窗口里表现为多条消息的原因。

    引用只加在第一个批次上：整轮回复在 QQ 里是连续的多条气泡，逐条都挂引用会
    让聊天窗口被引用框刷屏，首条点明在回谁就够了。

    :param text_segments: 已按打字习惯切分的气泡文本。
    :param emoji_refs: 已通过启动哈希校验的本地图片引用。
    :param emoji_sub_types: 与引用逐项对齐的 OneBot 表情包子类型。
    :param quote_message_id: 第一个批次要引用的平台消息编号；空字符串表示不引用。
    :return: 按文字、表情包原始顺序排列的非空消息段数组。
    :raises ValueError: 消息为空、字段数量不一致或图片字段不合法。
    """

    segments = outbound_message_segments(
        text_segments,
        emoji_refs,
        emoji_sub_types,
    )
    batches = [[segment] for segment in segments]
    if quote_message_id and batches:
        batches[0].insert(0, reply_segment(quote_message_id))
    return batches


def _image_segment(
    source: str,
    source_kind: ImageSourceKind,
    *,
    sub_type: int | None = None,
) -> Dict[str, Any]:
    """构造带来源协议前缀的 OneBot 图片段。

    :param source: 图片内容或本地路径；可带也可不带对应的 `base64://` 或 `file://` 前缀。
    :param source_kind: 来源类型，只能为 `base64` 或 `file`。
    :param sub_type: 可选的 OneBot 图片子类型。
    :return: 形如 `{'type': 'image', 'data': {'file': '...'}}` 的新字典；提供子类型时
        同时包含 ``data.sub_type``。
    :raises ValueError: 来源为空、错误地使用另一种来源前缀，或子类型不合法。
    副作用：不读取文件、不编码内容，也不修改输入字符串。
    """
    value = _required_value(source, '图片来源不能为空')
    expected_prefix = f'{source_kind}://'
    other_kind = 'file' if source_kind == 'base64' else 'base64'
    other_prefix = f'{other_kind}://'
    if value.startswith(other_prefix):
        raise ValueError(f'{source_kind} 图片不能使用 {other_prefix} 前缀')
    if not value.startswith(expected_prefix):
        value = expected_prefix + value
    data: Dict[str, Any] = {'file': value}
    normalized_sub_type = _image_sub_type(sub_type)
    if normalized_sub_type is not None:
        data['sub_type'] = normalized_sub_type
    return {'type': 'image', 'data': data}


def _required_emoji_sub_type(value: Any) -> int:
    """规范化出站表情包子类型，并拒绝普通图片子类型。"""

    sub_type = _image_sub_type(value)
    if sub_type is None or sub_type in _IMAGE_SUBTYPES_THAT_ARE_NOT_EMOJI:
        raise ValueError(f'表情包 sub_type 不合法：{value!r}')
    return sub_type


def _image_sub_type(value: Any) -> int | None:
    """把 OneBot 图片子类型规范化为非负整数；缺失时返回 ``None``。"""

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('图片段 data.sub_type 必须是非负整数')
    if isinstance(value, int):
        sub_type = value
    elif isinstance(value, str) and value.strip().isdigit():
        sub_type = int(value.strip())
    else:
        raise ValueError('图片段 data.sub_type 必须是非负整数')
    if sub_type < 0:
        raise ValueError('图片段 data.sub_type 必须是非负整数')
    return sub_type


def _segment_type(segment: Segment) -> str:
    """读取并规范化消息段类型。

    :param segment: OneBot 消息段映射。
    :return: 去除首尾空白的 `type` 字段。
    :raises ValueError: `type` 缺失、不是字符串或为空白。
    副作用：不修改消息段。
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
    副作用：不修改消息段。
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
    副作用：不修改输入值。
    """
    normalized = _string_value(value)
    if not normalized:
        raise ValueError(message)
    return normalized


def _string_value(value: Any) -> str:
    """将协议值转换为空安全的字符串。

    :param value: 任意值；`None` 表示缺失。
    :return: `None` 对应空字符串，否则返回去除首尾空白的字符串表示。
    副作用：不执行 I/O。
    """
    if value is None:
        return ''
    return str(value).strip()
