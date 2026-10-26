"""验证 QQ 合并转发从 OneBot 结构到平台中立消息树的解析。"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from src.core.platform_io.forward import (
    ForwardMessagePart,
    ForwardMessageTree,
    ForwardNode,
    forward_tree_from_payload,
    forward_tree_to_payload,
)
from src.platforms.onebot11.forward import (
    FORWARD_PREVIEW_MAX_CHARS,
    forward_tree_preview,
    parse_forward_content,
    parse_forward_response,
    unreadable_forward_tree,
)


def _message(sender: str, segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """构造一条协议端转发节点消息。"""
    return {
        'sender': {'nickname': sender, 'card': ''},
        'message': segments,
    }


def _nested_messages(depth: int) -> List[Dict[str, Any]]:
    """构造指定深度、每层都保留前后文本的嵌套转发树。"""
    messages = [_message('最深层', [{'type': 'text', 'data': {'text': '叶子正文'}}])]
    for level in reversed(range(depth)):
        messages = [_message(
            f'第{level}层',
            [
                {'type': 'text', 'data': {'text': f'前{level}'}},
                {
                    'type': 'forward',
                    'data': {'id': f'nested-{level}', 'content': messages},
                },
                {'type': 'text', 'data': {'text': f'后{level}'}},
            ],
        )]
    return messages


class _Resolver:
    """记录调用次数的嵌套转发取内容桩；值为异常实例时表示该层取内容失败。"""

    def __init__(self, responses: Dict[str, Any] | None = None) -> None:
        self._responses = responses or {}
        self.requested: List[str] = []

    async def __call__(self, forward_id: str) -> Dict[str, Any]:
        self.requested.append(forward_id)
        if forward_id not in self._responses:
            raise AssertionError(f'未预期的嵌套转发编号：{forward_id}')
        result = self._responses[forward_id]
        if isinstance(result, Exception):
            raise result
        return result


def _only_nested(tree: ForwardMessageTree) -> ForwardMessageTree:
    """返回当前树唯一的直接嵌套转发。"""
    nested = [
        part.nested
        for node in tree.nodes
        for part in node.parts
        if part.kind == 'forward'
    ]
    assert len(nested) == 1
    assert nested[0] is not None
    return nested[0]


async def test_parse_forward_response_preserves_mixed_content_order() -> None:
    """嵌套转发前后的正文和媒体占位必须保持协议顺序。"""
    response = {
        'data': {
            'messages': [_message(
                '昵称',
                [
                    {'type': 'text', 'data': {'text': '转发前'}},
                    {
                        'type': 'forward',
                        'data': {
                            'id': 'inner',
                            'content': [_message(
                                '内层用户',
                                [{'type': 'text', 'data': {'text': '内层正文'}}],
                            )],
                        },
                    },
                    {'type': 'image', 'data': {'file': 'a.jpg', 'sub_type': 0}},
                    {'type': 'text', 'data': {'text': '转发后'}},
                ],
            )],
        },
    }

    tree = await parse_forward_response(response, _Resolver())

    assert tree.nodes[0].sender_name == '昵称'
    assert [part.kind for part in tree.nodes[0].parts] == [
        'text', 'forward', 'image', 'text',
    ]
    assert tree.nodes[0].parts[0].text == '转发前'
    assert tree.nodes[0].parts[2].text == '[图片]'
    assert tree.nodes[0].parts[2].source == 'a.jpg'
    assert tree.nodes[0].parts[3].text == '转发后'
    nested = tree.nodes[0].parts[1].nested
    assert nested is not None
    assert nested.nodes[0].parts == (ForwardMessagePart.text_part('内层正文'),)


async def test_parse_forward_content_supports_many_nested_levels() -> None:
    """解析器不设置很浅的层数上限，至少可保真处理 64 层嵌套。"""
    tree = await parse_forward_content(_nested_messages(64), _Resolver())

    current = tree
    for level in range(64):
        assert current.nodes[0].sender_name == f'第{level}层'
        assert current.nodes[0].parts[0].text == f'前{level}'
        assert current.nodes[0].parts[-1].text == f'后{level}'
        current = _only_nested(current)
    assert current.nodes[0].sender_name == '最深层'
    assert current.nodes[0].parts[0].text == '叶子正文'


async def test_forward_tree_payload_round_trip_keeps_deep_structure() -> None:
    """跨适配器 HTTP 边界序列化后，嵌套层级与文本不得丢失。"""
    original = await parse_forward_content(_nested_messages(32), _Resolver())

    restored = forward_tree_from_payload(forward_tree_to_payload(original))

    assert restored == original


@pytest.mark.parametrize(
    'response, message',
    [
        ({}, r'实际类型 NoneType'),
        ({'data': {'messages': {}}}, r'顶层键 messages'),
        ({'data': {'messages': []}}, '节点数组不能为空'),
        ({'data': []}, '节点数组不能为空'),
    ],
)
async def test_parse_forward_response_rejects_malformed_shape(
    response: Dict[str, Any],
    message: str,
) -> None:
    """协议结构不完整时应精准暴露，不能伪造空转发内容。"""
    with pytest.raises(ValueError, match=message):
        await parse_forward_response(response, _Resolver())


def _shape_messages() -> List[Dict[str, Any]]:
    """构造用于响应形状识别对比的两节点数组。"""
    return [
        _message('甲', [{'type': 'text', 'data': {'text': '第一条'}}]),
        _message('乙', [{'type': 'text', 'data': {'text': '第二条'}}]),
    ]


# 同类协议端不同版本下 get_forward_msg 已知的五种真实响应形状。
_SHAPE_RESPONSES = [
    {'data': _shape_messages()},
    {'data': {'messages': _shape_messages()}},
    {'data': {'content': _shape_messages()}},
    {'data': {'data': {'messages': _shape_messages()}}},
    {'data': {'data': {'content': _shape_messages()}}},
]


@pytest.mark.parametrize(
    'response',
    _SHAPE_RESPONSES,
    ids=['data-array', 'data-messages', 'data-content', 'data-data-messages', 'data-data-content'],
)
async def test_parse_forward_response_accepts_known_response_shapes(
    response: Dict[str, Any],
) -> None:
    """五种真实存在的响应形状应解析出同一棵树。"""
    tree = await parse_forward_response(response, _Resolver())

    assert [node.sender_name for node in tree.nodes] == ['甲', '乙']
    assert tree.nodes[0].parts[0].text == '第一条'
    assert tree.nodes[1].parts[0].text == '第二条'


@pytest.mark.parametrize(
    'response',
    _SHAPE_RESPONSES,
    ids=['data-array', 'data-messages', 'data-content', 'data-data-messages', 'data-data-content'],
)
async def test_parse_forward_response_rejects_empty_node_array(
    response: Dict[str, Any],
) -> None:
    """命中已知形状但节点数组为空时仍须报错，空转发不能伪装成解析成功。"""

    def _emptied(value: Any) -> Any:
        if isinstance(value, list):
            return []
        if isinstance(value, dict):
            return {key: _emptied(item) for key, item in value.items()}
        return value

    with pytest.raises(ValueError, match='节点数组不能为空'):
        await parse_forward_response(_emptied(response), _Resolver())


async def test_parse_forward_response_reports_unrecognized_shape_details() -> None:
    """五种形状都对不上时，报错须携带 data 的实际类型与顶层键名。"""
    with pytest.raises(ValueError, match=r'实际类型 int'):
        await parse_forward_response({'data': 42}, _Resolver())
    with pytest.raises(ValueError, match=r'顶层键.*items'):
        await parse_forward_response(
            {'data': {'items': _shape_messages()}},
            _Resolver(),
        )


async def test_nested_forward_without_content_is_fetched_by_id() -> None:
    """协议端不内联内层正文时，按资源编号补取而不是丢弃整棵树。"""
    messages = [_message(
        '外层',
        [{'type': 'forward', 'data': {'id': 'inner-id'}}],
    )]
    resolver = _Resolver({'inner-id': {'data': {'messages': [_message(
        '内层用户',
        [{'type': 'text', 'data': {'text': '补取到的正文'}}],
    )]}}})

    tree = await parse_forward_content(messages, resolver)

    assert resolver.requested == ['inner-id']
    nested = _only_nested(tree)
    assert nested.nodes[0].sender_name == '内层用户'
    assert nested.nodes[0].parts[0].text == '补取到的正文'


async def test_nested_forward_without_content_or_id_is_rejected() -> None:
    """内层既没有正文也没有编号时无从补取，必须精确失败。"""
    messages = [_message('外层', [{'type': 'forward', 'data': {}}])]

    with pytest.raises(ValueError, match='既没有 data.content 也没有 data.id'):
        await parse_forward_content(messages, _Resolver())


async def test_nested_forward_self_reference_is_rejected() -> None:
    """自引用的资源编号必须在再次请求协议端之前断链。"""
    loop_response = {'data': {'messages': [_message(
        '外层',
        [{'type': 'forward', 'data': {'id': 'loop-id'}}],
    )]}}
    messages = [_message('根', [{'type': 'forward', 'data': {'id': 'loop-id'}}])]
    resolver = _Resolver({'loop-id': loop_response})

    with pytest.raises(ValueError, match='出现循环引用'):
        await parse_forward_content(messages, resolver)

    assert resolver.requested == ['loop-id']


async def test_nested_forward_fetch_failure_degrades_to_text_part() -> None:
    """补取抛异常时该嵌套层降级为文本片段，外层与同层其他内容不受影响。"""
    messages = [_message('外层', [
        {'type': 'text', 'data': {'text': '前文'}},
        {'type': 'forward', 'data': {'id': 'bad-inner'}},
        {'type': 'text', 'data': {'text': '后文'}},
    ])]
    resolver = _Resolver({'bad-inner': RuntimeError('协议端超时')})

    tree = await parse_forward_content(messages, resolver)

    assert resolver.requested == ['bad-inner']
    assert [part.kind for part in tree.nodes[0].parts] == ['text', 'text', 'text']
    assert tree.nodes[0].parts[0].text == '前文'
    assert tree.nodes[0].parts[1].text == '[这一层的转发内容读取失败]'
    assert tree.nodes[0].parts[2].text == '后文'


async def test_nested_forward_invalid_fetched_structure_degrades_to_text_part() -> None:
    """补取回来的结构非法时同样降级，不丢弃整棵根树。"""
    messages = [_message('外层', [{'type': 'forward', 'data': {'id': 'inner'}}])]
    resolver = _Resolver({'inner': {'data': {'foo': 1}}})

    tree = await parse_forward_content(messages, resolver)

    assert tree.nodes[0].parts[0].kind == 'text'
    assert tree.nodes[0].parts[0].text == '[这一层的转发内容读取失败]'


async def test_nested_degrade_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    """降级必须留痕：只在正文里少一块内容、日志里毫无痕迹是不可查的。

    2026-09-01 那次转发读不了，定案唯一的线索就是适配器控制台里这条 error 原文；
    降级把异常吃掉之后若不记日志，现场只剩「内容少了一块」，连是哪个资源编号
    取失败都无从查起。日志走 structlog 直接写标准输出，不经 stdlib handler，
    因此这里读 capsys 而不是 caplog。
    """
    messages = [_message('外层', [{'type': 'forward', 'data': {'id': 'bad-inner'}}])]
    resolver = _Resolver({'bad-inner': RuntimeError('协议端超时')})

    await parse_forward_content(messages, resolver)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert 'bad-inner' in output
    assert '协议端超时' in output


async def test_deep_circular_reference_still_propagates() -> None:
    """补取内容里的深层循环引用属结构错误，须穿过逐层降级的捕获上抛。"""
    loop_messages = [_message('外层', [{'type': 'forward', 'data': {'id': 'loop'}}])]
    response = {'data': {'messages': loop_messages}}
    messages = [_message('根', [{'type': 'forward', 'data': {'id': 'outer'}}])]
    resolver = _Resolver({'outer': response, 'loop': response})

    with pytest.raises(ValueError, match='出现循环引用'):
        await parse_forward_content(messages, resolver)

    assert resolver.requested == ['outer', 'loop']


def test_unreadable_forward_tree_is_distinct_from_real_nodes() -> None:
    """占位树与真实节点可区分，不伪造发送者姓名。"""
    tree = unreadable_forward_tree()

    node = tree.nodes[0]
    assert node.sender_name == '[读取失败]'
    assert node.parts[0].kind == 'text'
    assert node.parts[0].text == '[这一层的转发内容读取失败]'


def test_forward_tree_preview_renders_nodes_and_real_total() -> None:
    """预览按节点顺序渲染发送者与正文，末尾缀真实总条数。"""
    tree = ForwardMessageTree(nodes=(
        ForwardNode(
            sender_name='张三',
            parts=(ForwardMessagePart.text_part('今天这个更新有点离谱'),),
        ),
        ForwardNode(
            sender_name='李四',
            parts=(ForwardMessagePart.text_part('确实，我这边直接崩了'),),
        ),
    ))

    assert forward_tree_preview(tree) == (
        '[转发消息：张三：今天这个更新有点离谱｜李四：确实，我这边直接崩了｜共 2 条]'
    )


def test_forward_tree_preview_compresses_newlines_and_marks_nested() -> None:
    """节点正文换行压成空格，嵌套转发片段渲染为短标记，保持单行。"""
    tree = ForwardMessageTree(nodes=(
        ForwardNode(
            sender_name='张三',
            parts=(
                ForwardMessagePart.text_part('第一行\n第二行\t带制表'),
                ForwardMessagePart.forward_part(ForwardMessageTree(nodes=(
                    ForwardNode(
                        sender_name='内层',
                        parts=(ForwardMessagePart.text_part('内层正文'),),
                    ),
                ))),
                ForwardMessagePart.text_part('尾段'),
            ),
        ),
    ))

    preview = forward_tree_preview(tree)

    assert preview == '[转发消息：张三：第一行 第二行 带制表[嵌套转发]尾段｜共 1 条]'
    assert '\n' not in preview


def test_forward_tree_preview_stops_at_budget_without_partial_nodes() -> None:
    """超预算时装不下的节点整体缺席，无省略号与截半节点，总数仍为真实值。"""
    tree = ForwardMessageTree(nodes=tuple(
        ForwardNode(
            sender_name=f'节点{index}',
            parts=(ForwardMessagePart.text_part(f'第{index}条内容' + '细' * 24),),
        )
        for index in range(1, 6)
    ))

    preview = forward_tree_preview(tree)

    assert len(preview) <= FORWARD_PREVIEW_MAX_CHARS
    assert preview.endswith('｜共 5 条]')
    assert '…' not in preview
    # 装下的节点必须完整出现，从第一个装不下的节点起整体缺席。
    assert f'节点1：第1条内容' + '细' * 24 in preview
    assert f'节点2：第2条内容' + '细' * 24 in preview
    assert f'节点3：第3条内容' + '细' * 24 in preview
    assert '节点4' not in preview
    assert '节点5' not in preview


def test_forward_tree_preview_without_fitting_nodes_keeps_total_only() -> None:
    """预算内一个节点都装不下时，预览仅保留真实总条数。"""
    tree = ForwardMessageTree(nodes=(
        ForwardNode(
            sender_name='张三',
            parts=(ForwardMessagePart.text_part('长' * 200),),
        ),
        ForwardNode(
            sender_name='李四',
            parts=(ForwardMessagePart.text_part('短'),),
        ),
    ))

    assert forward_tree_preview(tree) == '[转发消息：共 2 条]'


@pytest.mark.parametrize(
    'sender, message',
    [
        (None, '缺少对象类型的 sender'),
        ({'card': ' ', 'nickname': '', 'user_id': None}, 'sender 缺少非空'),
    ],
)
async def test_forward_node_requires_sender_identity(
    sender: Any,
    message: str,
) -> None:
    """发送者结构损坏时不能用“未知用户”掩盖协议错误。"""
    messages = [{
        'sender': sender,
        'message': [{'type': 'text', 'data': {'text': '正文'}}],
    }]

    with pytest.raises(ValueError, match=message):
        await parse_forward_content(messages, _Resolver())


def _image_segment(name: str) -> Dict[str, Any]:
    """构造一个带直链的普通图片段。"""
    return {
        'type': 'image',
        'data': {
            'file': f'{name}.jpg',
            'url': f'https://gchat.qpic.cn/{name}?rkey=r',
            'sub_type': 0,
        },
    }


def _video_segment(name: str) -> Dict[str, Any]:
    """构造一个带直链与内容文件名的视频段。"""
    return {
        'type': 'video',
        'data': {
            'file': f'{name}.mp4',
            'url': f'https://multimedia.nt.qq.com.cn/{name}?rkey=r',
        },
    }


def _media_layer(level: int, nested: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """构造一层同时含文本、图片、视频（以及可选嵌套段）的转发节点数组。"""
    segments: List[Dict[str, Any]] = [
        {'type': 'text', 'data': {'text': f'第{level}层'}},
        _image_segment(f'img{level}'),
        _video_segment(f'vid{level}'),
    ]
    if nested is not None:
        segments.append(nested)
    return [_message(f'用户{level}', segments)]


def _media_parts(tree: ForwardMessageTree) -> List[ForwardMessagePart]:
    """取一层里的媒体片段，保持原顺序。"""
    return [
        part
        for node in tree.nodes
        for part in node.parts
        if part.kind in ('image', 'video')
    ]


def _assert_media_layer(tree: ForwardMessageTree, level: int) -> None:
    """断言一层的图片与视频片段带着各自的占位与来源。"""
    image, video = _media_parts(tree)
    assert image == ForwardMessagePart.image_part(
        '[图片]', f'https://gchat.qpic.cn/img{level}?rkey=r',
    )
    assert video == ForwardMessagePart.video_part(
        '[视频]', f'https://multimedia.nt.qq.com.cn/vid{level}?rkey=r', f'vid{level}.mp4',
    )


async def test_inline_nested_layers_keep_image_and_video_sources() -> None:
    """内联嵌套的每一层都保留图片与视频来源，层数不设上限。"""
    messages = _media_layer(3, None)
    for level in (2, 1):
        messages = _media_layer(level, {'type': 'forward', 'data': {'content': messages}})
    messages = _media_layer(0, {'type': 'forward', 'data': {'content': messages}})

    tree = await parse_forward_content(messages, _Resolver())

    current = tree
    for level in range(4):
        _assert_media_layer(current, level)
        if level < 3:
            current = _only_nested(current)


async def test_nested_layers_fetched_by_id_keep_image_and_video_sources() -> None:
    """按编号补取的嵌套层同样保留媒体来源，补取多层时每层都不丢。"""
    resolver = _Resolver({
        'level-1': {'data': {'messages': _media_layer(
            1, {'type': 'forward', 'data': {'id': 'level-2'}},
        )}},
        'level-2': {'data': {'messages': _media_layer(
            2, {'type': 'forward', 'data': {'id': 'level-3'}},
        )}},
        'level-3': {'data': {'messages': _media_layer(3, None)}},
    })

    tree = await parse_forward_content(
        _media_layer(0, {'type': 'forward', 'data': {'id': 'level-1'}}),
        resolver,
    )

    assert resolver.requested == ['level-1', 'level-2', 'level-3']
    current = tree
    for level in range(4):
        _assert_media_layer(current, level)
        if level < 3:
            current = _only_nested(current)


async def test_emoji_and_media_without_source_stay_text_placeholders() -> None:
    """表情包保持现有文本占位；缺来源的图片与视频无从识别，也保持文本占位。"""
    tree = await parse_forward_content([_message('用户', [
        {'type': 'image', 'data': {'file': 'e.gif', 'url': 'https://gchat.qpic.cn/e', 'sub_type': 1}},
        {'type': 'image', 'data': {'sub_type': 0}},
        {'type': 'video', 'data': {'file': 'v.mp4'}},
    ])], _Resolver())

    assert tree.nodes[0].parts == (
        ForwardMessagePart.text_part('[表情包]'),
        ForwardMessagePart.text_part('[图片]'),
        ForwardMessagePart.text_part('[视频]'),
    )


def test_preview_keeps_media_placeholder_count() -> None:
    """媒体片段在单行预览里与改动前的文本占位逐字相同，[图片] / [视频] 个数不变。"""
    def tree_of(parts: Tuple[ForwardMessagePart, ...]) -> ForwardMessageTree:
        return ForwardMessageTree(nodes=(ForwardNode(sender_name='甲', parts=parts),))

    with_media = tree_of((
        ForwardMessagePart.text_part('看'),
        ForwardMessagePart.image_part('[图片]', 'https://gchat.qpic.cn/a'),
        ForwardMessagePart.video_part('[视频]', 'https://multimedia.nt.qq.com.cn/v', 'v.mp4'),
        ForwardMessagePart.image_part('[图片]', 'https://gchat.qpic.cn/b'),
    ))
    as_text = tree_of((
        ForwardMessagePart.text_part('看'),
        ForwardMessagePart.text_part('[图片]'),
        ForwardMessagePart.text_part('[视频]'),
        ForwardMessagePart.text_part('[图片]'),
    ))

    preview = forward_tree_preview(with_media)

    assert preview == forward_tree_preview(as_text)
    assert preview == '[转发消息：甲：看[图片][视频][图片]｜共 1 条]'


async def test_payload_round_trip_keeps_media_at_every_depth() -> None:
    """跨 HTTP 边界序列化后，每一层的图片与视频来源都原样还原。"""
    messages = _media_layer(2, None)
    messages = _media_layer(1, {'type': 'forward', 'data': {'content': messages}})
    original = await parse_forward_content(
        _media_layer(0, {'type': 'forward', 'data': {'content': messages}}),
        _Resolver(),
    )

    payload = forward_tree_to_payload(original)

    assert payload['nodes'][0]['parts'][1] == {
        'kind': 'image', 'text': '[图片]', 'source': 'https://gchat.qpic.cn/img0?rkey=r',
    }
    assert payload['nodes'][0]['parts'][2] == {
        'kind': 'video',
        'text': '[视频]',
        'source': 'https://multimedia.nt.qq.com.cn/vid0?rkey=r',
        'file': 'vid0.mp4',
    }
    assert forward_tree_from_payload(payload) == original


@pytest.mark.parametrize(
    'part, message',
    [
        (
            {'kind': 'image', 'text': '[图片]', 'source': 'https://a', 'file': 'a.jpg'},
            '未知字段：file',
        ),
        (
            {'kind': 'image', 'text': '[图片]', 'source': 'https://a', 'nested': {}},
            '未知字段：nested',
        ),
        ({'kind': 'image', 'text': '[图片]', 'source': ''}, '缺少非空 source'),
        ({'kind': 'image', 'source': 'https://a'}, '缺少非空 text'),
        (
            {'kind': 'video', 'text': '[视频]', 'source': 'https://v', 'file': 'v.mp4', 'url': 'x'},
            '未知字段：url',
        ),
        ({'kind': 'video', 'text': '[视频]', 'source': 'https://v'}, '缺少字符串类型的 file'),
        ({'kind': 'video', 'text': '[视频]', 'source': ' ', 'file': ''}, '缺少非空 source'),
        ({'kind': 'audio', 'text': '[语音]'}, 'kind 不受支持：audio'),
    ],
)
def test_payload_rejects_malformed_media_parts(part: Dict[str, Any], message: str) -> None:
    """媒体片段的载荷与文本片段同样严格：未知字段、缺字段、空来源一律拒绝。"""
    payload = {'nodes': [{'senderName': '甲', 'parts': [part]}]}

    with pytest.raises(ValueError, match=message):
        forward_tree_from_payload(payload)


def test_media_part_rejects_mixed_shapes() -> None:
    """数据类本身拒绝把媒体字段塞进文本或嵌套片段，以及缺来源的媒体片段。"""
    leaf = ForwardMessageTree(nodes=(
        ForwardNode(sender_name='甲', parts=(ForwardMessagePart.text_part('正文'),)),
    ))

    with pytest.raises(ValueError, match='文本片段不能携带 source 或 file'):
        ForwardMessagePart(kind='text', text='[图片]', source='https://a')
    with pytest.raises(ValueError, match='嵌套片段不能携带 source 或 file'):
        ForwardMessagePart(kind='forward', nested=leaf, file='v.mp4')
    with pytest.raises(ValueError, match='图片片段缺少来源'):
        ForwardMessagePart.image_part('[图片]', ' ')
    with pytest.raises(ValueError, match='图片片段不能携带 file'):
        ForwardMessagePart(kind='image', text='[图片]', source='https://a', file='a.jpg')
    with pytest.raises(ValueError, match='视频片段不能携带 nested'):
        ForwardMessagePart(kind='video', text='[视频]', source='https://v', nested=leaf)
