"""普通图片出站契约的验收：从 OutboundMessage 一路到 OneBot 消息段。

这条路是为开发者命令 ``/inst`` 的折线图开的，与表情包各走各的：
- 现象：带 ``sub_type`` 的 OneBot 图片段会被 QQ 客户端当成贴纸／表情显示。
- 原因：OneBot 靠 ``sub_type`` 有无区分两者，段结构本身完全相同。
- 后果：合并成一个字段后，折线图会以表情形式出现在群里，而这只能从聊天窗口看出来。

因此本包逐层断言两者不串：类型层、驱动载荷、适配器解析、消息段组装。
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from src.core.platform_io.drivers.qq_ws import QqWebSocketDriver
from src.core.platform_io.types import OutboundMessage, StreamRef
from src.platforms.onebot11.backend import BackendOutbound, _parse_outbound
from src.platforms.onebot11.runner import _batch_delays_seconds
from src.platforms.onebot11.segments import outbound_message_batches, outbound_message_segments

GROUP_STREAM_ID = 2
CHART_PATH = 'D:/data/charts/inst-d.png'


def _stream() -> StreamRef:
    """构造一个 QQ 群 stream 引用。"""
    return StreamRef(id=GROUP_STREAM_ID, platform='qq', kind='group', external_id='629201002')


def test_打字停顿要覆盖图片批次() -> None:
    """图片各占一个发送批次，停顿数量不含它们时属于协议不一致，必须当场抛。"""
    with pytest.raises(ValueError, match='发送批次'):
        OutboundMessage(
            stream=_stream(),
            segments=['一句话'],
            image_refs=(CHART_PATH,),
            batch_delays_ms=(0,),
        )

    message = OutboundMessage(
        stream=_stream(),
        segments=['一句话'],
        image_refs=(CHART_PATH,),
        batch_delays_ms=(0, 300),
    )
    assert message.image_refs == (CHART_PATH,)


@pytest.mark.asyncio
async def test_驱动只在有图时才写字段() -> None:
    """imageRefs 缺省不下发，保持既有精确载荷断言不变。"""
    sent: list[Dict[str, Any]] = []

    async def push(stream_id: int, event: str, payload: Any) -> int:
        del stream_id, event
        sent.append(payload)
        return 1

    driver = QqWebSocketDriver(push)

    await driver.send(OutboundMessage(
        stream=_stream(), segments=['装机量 1284'], image_refs=(CHART_PATH,),
    ))
    await driver.send(OutboundMessage(stream=_stream(), segments=['没有图']))

    assert sent[0]['imageRefs'] == [CHART_PATH]
    assert 'emojiRefs' not in sent[0]
    assert 'imageRefs' not in sent[1]


def test_适配器解析图片路径并去空白() -> None:
    """字段缺省时为空元组；空字符串是协议错误，不静默丢弃。"""
    body = {
        'streamKind': 'group',
        'streamExternalId': '629201002',
        'segments': ['装机量 1284'],
    }

    with_images = _parse_outbound({
        'stream_id': GROUP_STREAM_ID,
        'payload': {**body, 'imageRefs': [f' {CHART_PATH} ']},
    })
    without_images = _parse_outbound({'stream_id': GROUP_STREAM_ID, 'payload': body})

    assert with_images.image_refs == (CHART_PATH,)
    assert without_images.image_refs == ()

    with pytest.raises(ValueError, match='imageRefs'):
        _parse_outbound({
            'stream_id': GROUP_STREAM_ID,
            'payload': {**body, 'imageRefs': ['']},
        })


def test_只有图片没有文字也是合法出站() -> None:
    """空文字加一张图仍应发出；三者全空才是错误。"""
    parsed = _parse_outbound({
        'stream_id': GROUP_STREAM_ID,
        'payload': {
            'streamKind': 'group',
            'streamExternalId': '629201002',
            'segments': [],
            'imageRefs': [CHART_PATH],
        },
    })

    assert parsed.image_refs == (CHART_PATH,)

    with pytest.raises(ValueError, match='文本、表情包或图片'):
        _parse_outbound({
            'stream_id': GROUP_STREAM_ID,
            'payload': {
                'streamKind': 'group',
                'streamExternalId': '629201002',
                'segments': [],
            },
        })


def test_图片段不带_sub_type_表情包带() -> None:
    """这是两者唯一的结构差别，也是它们在客户端呈现不同的原因。"""
    segments = outbound_message_segments(
        ['装机量 1284'],
        ['D:/data/emojis/happy.png'],
        [1],
        [CHART_PATH],
    )

    assert segments[0] == {'type': 'text', 'data': {'text': '装机量 1284'}}
    assert segments[1]['data']['sub_type'] == 1
    assert 'sub_type' not in segments[2]['data']
    assert segments[2]['data']['file'] == f'file://{CHART_PATH}'


def test_每张图各成一个批次且引用只挂第一批() -> None:
    """三张图在群里是三个气泡；引用框只出现在第一条。"""
    batches = outbound_message_batches(
        ['装机量 1284'],
        [],
        [],
        '2145541855',
        [CHART_PATH, 'D:/data/charts/inst-n.png', 'D:/data/charts/inst-v.png'],
    )

    assert len(batches) == 4
    assert batches[0][0] == {'type': 'reply', 'data': {'id': '2145541855'}}
    assert all(len(batch) == 1 for batch in batches[1:])
    assert [batch[-1]['data']['file'] for batch in batches[1:]] == [
        f'file://{CHART_PATH}',
        'file://D:/data/charts/inst-n.png',
        'file://D:/data/charts/inst-v.png',
    ]


def test_适配器为图片批次补齐零停顿() -> None:
    """主体不下发停顿时全按 0 处理，否则图片批次会因为对不齐而发不出去。"""
    outbound = BackendOutbound(
        stream_id=GROUP_STREAM_ID,
        stream_kind='group',
        stream_external_id='629201002',
        segments=['装机量 1284'],
        image_refs=(CHART_PATH, 'D:/data/charts/inst-n.png'),
    )

    assert _batch_delays_seconds(outbound) == [0.0, 0.0, 0.0]
