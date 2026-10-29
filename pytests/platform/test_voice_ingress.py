"""验证原生转写的响应契约、片段定位、用户原话与失败可见性。"""

from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock

import asyncio

import pytest

from src.platforms.onebot11.backend import BackendOutbound
from src.platforms.onebot11.config import AdapterDocument
from src.platforms.onebot11.events import QqInboundEvent
from src.platforms.onebot11.runner import OneBot11Runner
from src.platforms.onebot11.transport import ActionError
from src.platforms.onebot11.voice import transcribe_inbound


def _event() -> QqInboundEvent:
    return QqInboundEvent(
        stream_kind='group', stream_external_id='10001', sender_external_id='20001',
        sender_nickname='小李', sender_group_card='', bot_name='月璃', text='[语音]',
        mentioned_me=True, external_message_id='123', authored_text='',
    )


async def test_transcription_rebuilds_only_record_segments() -> None:
    segments: List[Dict[str, Any]] = [
        {'type': 'reply', 'data': {'id': '9'}},
        {'type': 'text', 'data': {'text': '我手写的[语音]'}},
        {'type': 'at', 'data': {'qq': '555'}},
        {'type': 'record', 'data': {'file': 'voice.silk', 'url': 'https://example.test/a'}},
    ]
    transport = AsyncMock()
    transport.call_action.return_value = {'status': 'ok', 'data': {'text': ' 月璃，早上好 '}}
    event = await transcribe_inbound(
        transport, _event(), segments, supported=True,
        mention_names={'555': '月璃'}, quote_previews={'9': '引用：[语音]'},
    )
    assert event.text == '[引用：[语音]]我手写的[语音]@月璃[语音：月璃，早上好]'
    assert event.authored_text == '我手写的[语音]\n月璃，早上好'
    assert event.mentioned_me
    assert segments[-1]['type'] == 'record'
    transport.call_action.assert_awaited_once_with('get_record_text', {'message_id': '123'})


@pytest.mark.parametrize('response', [{'data': None}, {'data': {'text': ''}}, {'data': {'text': 1}}])
async def test_invalid_success_response_is_explicit_failure(response) -> None:
    transport = AsyncMock()
    transport.call_action.return_value = response
    event = await transcribe_inbound(
        transport, _event(), [{'type': 'record', 'data': {}}],
        supported=True, mention_names={}, quote_previews={},
    )
    assert event.text == '[语音：转写失败，内容未知]'
    assert event.authored_text == ''


async def test_unsupported_adapter_does_not_guess_action() -> None:
    transport = AsyncMock()
    event = await transcribe_inbound(
        transport, _event(), [{'type': 'record', 'data': {}}],
        supported=False, mention_names={}, quote_previews={},
    )
    assert '不支持转写' in event.text
    transport.call_action.assert_not_called()


async def test_timeout_is_visible_and_cancellation_propagates() -> None:
    transport = AsyncMock()
    transport.call_action.side_effect = asyncio.TimeoutError()
    args = (transport, _event(), [{'type': 'record', 'data': {}}])
    options = dict(supported=True, mention_names={}, quote_previews={})
    event = await transcribe_inbound(*args, **options)
    assert '转写失败' in event.text
    transport.call_action.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await transcribe_inbound(*args, **options)


async def test_multiple_records_are_not_assigned_same_transcript() -> None:
    transport = AsyncMock()
    event = await transcribe_inbound(
        transport, _event(), [{'type': 'record', 'data': {}}] * 2,
        supported=True, mention_names={}, quote_previews={},
    )
    assert event.text.count('转写失败') == 2
    transport.call_action.assert_not_called()


@pytest.mark.parametrize('kind,action,target', [
    ('direct', 'send_private_msg', 'user_id'), ('group', 'send_group_msg', 'group_id'),
])
async def test_runner_sends_record_and_reports_failure(kind, action, target) -> None:
    async def outbound():
        yield BackendOutbound(
            stream_id=1, stream_kind=kind, stream_external_id='10001',
            segments=['你好'], voice_data=('YXVkaW8=',), turn_id=20,
        )
        yield BackendOutbound(stream_id=1, stream_kind=kind, stream_external_id='10001', segments=['下一条'])

    backend = Mock(iter_outbound=outbound, report_delivery_failure=AsyncMock())
    transport = Mock(call_action=AsyncMock(side_effect=[
        ActionError(action, {'status': 'failed', 'retcode': 100, 'wording': '语音转码失败'}),
        {'status': 'ok', 'data': {'message_id': 2}},
    ]))
    runner = OneBot11Runner(Mock(spec=AdapterDocument), 1, 'test-token', backend=backend, transport=transport)
    await runner._consume_backend_outbound()
    assert transport.call_action.await_args_list[0].args == (
        action, {target: 10001, 'message': [{'type': 'record', 'data': {'file': 'base64://YXVkaW8='}}]},
    )
    assert transport.call_action.await_count == 2
    backend.report_delivery_failure.assert_awaited_once()
    assert backend.report_delivery_failure.await_args.kwargs['turn_id'] == 20


@pytest.mark.parametrize('allowed', [True, False])
async def test_runner_transcribes_only_allowed_messages(allowed: bool) -> None:
    config = AdapterDocument.model_validate({
        'inner': {'version': '0.1.0'},
        'napcat': {
            'enabled': True, 'self_qq': '13579', 'host': '127.0.0.1', 'port': 1,
            'token': 'test-token', 'reconnect_interval_sec': 1, 'action_timeout_sec': 1,
        },
        'owner': {'qq': '24680'},
        'private': {'mode': 'whitelist', 'list': []},
        'group': {'mode': 'whitelist', 'list': ['86420'] if allowed else []},
    })

    async def events():
        yield {
            'post_type': 'message', 'message_type': 'group', 'self_id': 13579,
            'message_id': 5001, 'group_id': 86420, 'user_id': 97531,
            'message': [{'type': 'record', 'data': {'file': 'test.silk'}}],
            'sender': {'user_id': 97531, 'nickname': '小李', 'card': ''},
        }

    transport = Mock(iter_events=events, call_action=AsyncMock(return_value={'data': {'text': '月璃早安'}}))
    backend = Mock(submit_inbound=AsyncMock())
    runner = OneBot11Runner(config, 1, 'test-token', backend=backend, transport=transport)
    runner._capabilities = {'transcribe_record'}
    runner._group_name_attempted.add('86420')
    await runner._consume_protocol_events('13579', '月璃')
    if allowed:
        transport.call_action.assert_awaited_once_with('get_record_text', {'message_id': '5001'})
        event = backend.submit_inbound.await_args.args[0]
        assert event.text == '[语音：月璃早安]'
        assert event.authored_text == '月璃早安'
    else:
        transport.call_action.assert_not_called()
        backend.submit_inbound.assert_not_called()
