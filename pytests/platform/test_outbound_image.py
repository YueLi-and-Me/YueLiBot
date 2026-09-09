"""普通图片出站契约的验收：从 OutboundMessage 一路到 OneBot 消息段。

这条路是为开发者命令 ``/inst`` 的折线图开的，与表情包各走各的：
- 现象：带 ``sub_type`` 的 OneBot 图片段会被 QQ 客户端当成贴纸／表情显示。
- 原因：OneBot 靠 ``sub_type`` 有无区分两者，段结构本身完全相同。
- 后果：合并成一个字段后，折线图会以表情形式出现在群里，而这只能从聊天窗口看出来。

因此本包逐层断言两者不串：类型层、驱动载荷、适配器解析、消息段组装。
"""

from __future__ import annotations

from base64 import b64decode
from pathlib import Path
from structlog.testing import capture_logs
from typing import Any, AsyncIterator, Dict
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.platform_io.drivers.qq_ws import QqWebSocketDriver
from src.core.platform_io.types import OutboundMessage, StreamRef
from src.platforms.onebot11.backend import BackendOutbound, _parse_outbound
from src.platforms.onebot11.config import AdapterDocument
from src.platforms.onebot11.runner import OneBot11Runner, _batch_delays_seconds
from src.platforms.onebot11.segments import (
    MAX_OUTBOUND_IMAGE_BYTES,
    outbound_message_batches,
    outbound_message_segments,
)
from src.platforms.onebot11.transport import ActionError

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


def test_图片段不带_sub_type_表情包带(tmp_path: Path) -> None:
    """这是两者唯一的结构差别，也是它们在客户端呈现不同的原因。"""
    emoji = tmp_path / '表情.gif'
    chart = tmp_path / '图表.png'
    emoji.write_bytes(b'GIF89a\x00\xff')
    chart.write_bytes(b'\x89PNG\r\n\x1a\n')
    segments = outbound_message_segments(
        ['装机量 1284'],
        [emoji.as_uri()],
        [1],
        [str(chart)],
    )

    assert segments[0] == {'type': 'text', 'data': {'text': '装机量 1284'}}
    assert segments[1]['data']['sub_type'] == 1
    assert 'sub_type' not in segments[2]['data']
    for segment, path in zip(segments[1:], (emoji, chart), strict=True):
        assert segment['data']['file'].startswith('base64://')
        assert b64decode(segment['data']['file'][9:], validate=True) == path.read_bytes()


def test_每张图各成一个批次且引用只挂第一批(tmp_path: Path) -> None:
    """三张图在群里是三个气泡；引用框只出现在第一条。"""
    paths = [tmp_path / name for name in ('inst-d.png', 'inst-n.png', 'inst-v.png')]
    for path in paths:
        path.write_bytes(path.name.encode('ascii'))
    batches = outbound_message_batches(
        ['装机量 1284'],
        [],
        [],
        '2145541855',
        [str(path) for path in paths],
    )

    assert len(batches) == 4
    assert batches[0][0] == {'type': 'reply', 'data': {'id': '2145541855'}}
    assert all(len(batch) == 1 for batch in batches[1:])
    assert [b64decode(batch[-1]['data']['file'][9:], validate=True) for batch in batches[1:]] == [
        path.read_bytes() for path in paths
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


@pytest.mark.parametrize('use_uri', [False, True])
def test_本地路径与转义_URI_均保持原始字节(tmp_path: Path, use_uri: bool) -> None:
    path = tmp_path / '表情 空格%23#.gif'
    content = bytes(range(256))
    path.write_bytes(content)
    reference = path.as_uri() if use_uri else str(path)
    segment = outbound_message_segments([], [], [], [reference])[0]
    assert segment['data']['file'].startswith('base64://')
    assert b64decode(segment['data']['file'][9:], validate=True) == content


@pytest.mark.parametrize('is_emoji', [False, True])
def test_恰好达到源字节上限可以发送(tmp_path: Path, is_emoji: bool) -> None:
    path = tmp_path / '边界.gif'
    content = b'x' * MAX_OUTBOUND_IMAGE_BYTES
    path.write_bytes(content)
    segments = outbound_message_segments(
        [], [path.as_uri()] if is_emoji else [], [7] if is_emoji else [],
        [] if is_emoji else [str(path)],
    )
    assert b64decode(segments[0]['data']['file'][9:], validate=True) == content
    assert segments[0]['data'].get('sub_type') == (7 if is_emoji else None)


@pytest.mark.parametrize('is_emoji', [False, True])
@pytest.mark.parametrize('failure', ['oversize', 'missing', 'permission', 'directory', 'empty'])
def test_任一图片准备失败就拒绝整条批次(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, is_emoji: bool, failure: str,
) -> None:
    path = tmp_path / '失败.gif'
    if failure == 'oversize':
        path.write_bytes(b'x' * (MAX_OUTBOUND_IMAGE_BYTES + 1))
    elif failure == 'directory':
        path.mkdir()
    elif failure == 'empty':
        path.touch()
    elif failure == 'permission':
        path.write_bytes(b'GIF89a')
        monkeypatch.setattr(Path, 'open', Mock(side_effect=PermissionError('测试拒绝读取')))
    # 前面已有文字也不能返回半条批次。
    with pytest.raises(ActionError) as raised:
        outbound_message_batches(
            ['这段文字不能先发'],
            [path.as_uri()] if is_emoji else [], [1] if is_emoji else [],
            image_refs=[] if is_emoji else [str(path)],
        )
    error = raised.value
    assert str(path) in str(error)
    assert error.action == 'prepare_image'
    assert error.response['status'] == 'local_error'
    assert 'retcode' not in error.response
    if failure == 'oversize':
        assert error.response['actual_bytes'] == MAX_OUTBOUND_IMAGE_BYTES + 1
        assert str(MAX_OUTBOUND_IMAGE_BYTES + 1) in str(error)
    elif failure == 'missing':
        assert isinstance(error.__cause__, FileNotFoundError)
    elif failure == 'permission':
        assert isinstance(error.__cause__, PermissionError)
    elif failure == 'directory':
        assert '不是常规文件' in str(error)
    else:
        assert '文件内容为空' in str(error)
        assert error.response['actual_bytes'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('stream_kind', ['group', 'direct'])
@pytest.mark.parametrize('failure', ['oversize', 'missing', 'permission', 'directory'])
async def test_准备失败记录日志且不发送半条消息并继续后续消息(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream_kind: str, failure: str,
) -> None:
    bad = tmp_path / '失败.png'
    good = tmp_path / '可读.gif'
    good.write_bytes(b'GIF89a')
    if failure == 'oversize':
        bad.write_bytes(b'x' * (MAX_OUTBOUND_IMAGE_BYTES + 1))
    elif failure == 'directory':
        bad.mkdir()
    elif failure == 'permission':
        bad.write_bytes(b'x')
        original_open = Path.open

        def deny_bad(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == bad:
                raise PermissionError('测试拒绝读取')
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'open', deny_bad)

    async def outbound() -> AsyncIterator[BackendOutbound]:
        yield BackendOutbound(
            stream_id=GROUP_STREAM_ID, stream_kind=stream_kind, stream_external_id='629201002',
            segments=['不能提前发送'], emoji_refs=(good.as_uri(),), emoji_sub_types=(7,),
            image_refs=(str(bad),), turn_id=19,
        )
        yield BackendOutbound(
            stream_id=GROUP_STREAM_ID, stream_kind=stream_kind, stream_external_id='629201002',
            segments=['后续消息正常'],
        )

    backend = Mock(iter_outbound=outbound, report_delivery_failure=AsyncMock())
    transport = Mock(call_action=AsyncMock())
    runner = OneBot11Runner(
        Mock(spec=AdapterDocument), backend_port=1, token='test-token-fixture',
        backend=backend, transport=transport,
    )
    with capture_logs() as logs:
        await runner._consume_backend_outbound()

    transport.call_action.assert_awaited_once_with(
        'send_group_msg' if stream_kind == 'group' else 'send_private_msg',
        {
            'group_id' if stream_kind == 'group' else 'user_id': 629201002,
            'message': [{'type': 'text', 'data': {'text': '后续消息正常'}}],
        },
    )
    backend.report_delivery_failure.assert_awaited_once()
    report = backend.report_delivery_failure.call_args.kwargs
    assert report['turn_id'] == 19
    assert str(bad) in report['error']
    failures = [entry for entry in logs if entry['event'] == 'QQ 消息发送失败']
    assert len(failures) == 1
    assert failures[0]['streamKind'] == stream_kind
    assert failures[0]['error'] == report['error']
    assert failures[0]['log_level'] == 'error'
