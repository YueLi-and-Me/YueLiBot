"""验证出站动作在协议端失败时能回报主体，且失败原因不被丢弃。

主体的投递回执只证明报文送到了适配器，真正的平台调用在适配器进程里才发出。
本模块覆盖三件事：协议端失败原因（message / wording）必须进入异常信息；出站
报文携带的回合编号必须原样解析；戳一戳、表情回应与消息发送失败时必须回报主体。

依赖 ``src.platforms.onebot11`` 的 transport / backend / runner 边界。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.platforms.onebot11.backend import (
    BackendPoke,
    BackendReaction,
    _parse_outbound,
    _parse_poke,
    _parse_reaction,
)
from src.platforms.onebot11.config import (
    GroupAccessConfig,
    ProtocolConnectionConfig,
    AdapterDocument,
    PrivateAccessConfig,
)
from src.platforms.onebot11.runner import OneBot11Runner
from src.platforms.onebot11.transport import ActionError


def _document() -> AdapterDocument:
    """构造一份仅供运行器实例化使用的最小配置。"""
    return AdapterDocument(
        inner={'version': '0.1.0'},
        owner={'qq': '24680'},
        napcat=ProtocolConnectionConfig(
            enabled=True,
            self_qq='13579',
            host='127.0.0.1',
            port=1,
            token='protocol-secret',
            reconnect_interval_sec=5.0,
            action_timeout_sec=15.0,
        ),
        private=PrivateAccessConfig(mode='blacklist', list=[]),
        group=GroupAccessConfig(mode='whitelist', list=['629201002']),
    )


class _FailingTransport:
    """所有 action 都按协议端拒绝失败的传输替身。"""

    def __init__(self, response: dict[str, Any]) -> None:
        """保存协议端返回的失败响应。"""
        self._response = response

    async def call_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """恒抛 :class:`ActionError`，模拟协议端明确拒绝。"""
        raise ActionError(action, self._response)


class _RecordingBackend:
    """记录投递失败回报的主体客户端替身。"""

    def __init__(self) -> None:
        """初始化空的回报记录。"""
        self.failures: list[dict[str, Any]] = []

    async def report_delivery_failure(self, **kwargs: Any) -> None:
        """记录一次回报调用的全部具名参数。"""
        self.failures.append(kwargs)


_PACKET_BACKEND_FAILURE = {
    'status': 'failed',
    'retcode': 1400,
    'message': 'packetBackend不可用，请检查packetBackend状态',
    'wording': 'packetBackend不可用，请检查packetBackend状态',
}


def test_action_error_keeps_protocol_reason() -> None:
    """协议端的失败原因必须进入异常信息，而不是只剩状态码。"""
    error = ActionError('group_poke', _PACKET_BACKEND_FAILURE)

    text = str(error)

    assert 'retcode=1400' in text
    assert 'packetBackend不可用' in text
    # message 与 wording 内容相同时只保留一份，避免同一句话重复两遍。
    assert text.count('packetBackend不可用') == 1


def test_action_error_without_reason_keeps_status_only() -> None:
    """协议端没有给出原因时，异常信息保持原有的状态码形态。"""
    error = ActionError('group_poke', {'status': 'failed', 'retcode': 1400})

    assert str(error) == "action group_poke 失败：status='failed' retcode=1400"


def test_outbound_payloads_carry_turn_id() -> None:
    """三条出站通道都必须原样解析主体下发的回合编号。"""
    send = _parse_outbound({
        'stream_id': 3,
        'payload': {
            'streamKind': 'group',
            'streamExternalId': '629201002',
            'segments': ['在的'],
            'turnId': 1275,
        },
    })
    poke = _parse_poke({
        'stream_id': 3,
        'payload': {
            'streamKind': 'group',
            'streamExternalId': '629201002',
            'targetExternalId': '3209184542',
            'turnId': 1275,
        },
    })
    react = _parse_reaction({
        'stream_id': 3,
        'payload': {
            'streamKind': 'group',
            'streamExternalId': '629201002',
            'targetExternalMessageId': '9312',
            'reaction': '赞',
            'turnId': 1275,
        },
    })

    assert (send.turn_id, poke.turn_id, react.turn_id) == (1275, 1275, 1275)


def test_missing_turn_id_defaults_to_zero() -> None:
    """缺省回合编号表示投递没有回合上下文，按 0 处理而不是报错。"""
    poke = _parse_poke({
        'stream_id': 3,
        'payload': {
            'streamKind': 'group',
            'streamExternalId': '629201002',
            'targetExternalId': '3209184542',
        },
    })

    assert poke.turn_id == 0


def test_invalid_turn_id_is_rejected() -> None:
    """回合编号存在但类型不对属于协议不同步，必须当场暴露。"""
    with pytest.raises(ValueError, match='turnId 必须是正整数'):
        _parse_poke({
            'stream_id': 3,
            'payload': {
                'streamKind': 'group',
                'streamExternalId': '629201002',
                'targetExternalId': '3209184542',
                'turnId': '1275',
            },
        })


@pytest.mark.asyncio
async def test_poke_failure_is_reported_to_backend() -> None:
    """戳一戳被协议端拒绝时必须回报主体，否则失败只留在适配器进程里。"""
    backend = _RecordingBackend()
    runner = OneBot11Runner(
        _document(),
        backend_port=1,
        token='backend-secret',
        transport=_FailingTransport(_PACKET_BACKEND_FAILURE),
        backend=backend,
    )

    await runner._apply_poke(BackendPoke(
        stream_id=3,
        stream_kind='group',
        stream_external_id='629201002',
        target_external_id='3209184542',
        turn_id=1275,
    ))

    assert len(backend.failures) == 1
    failure = backend.failures[0]
    assert failure['stream_id'] == 3
    assert failure['turn_id'] == 1275
    assert failure['action'] == 'poke'
    assert failure['target'] == '3209184542'
    assert 'packetBackend不可用' in failure['error']


@pytest.mark.asyncio
async def test_reaction_failure_is_reported_to_backend() -> None:
    """表情回应失败与戳一戳同口径回报，避免只有部分动作可见。"""
    backend = _RecordingBackend()
    runner = OneBot11Runner(
        _document(),
        backend_port=1,
        token='backend-secret',
        transport=_FailingTransport(_PACKET_BACKEND_FAILURE),
        backend=backend,
    )

    await runner._apply_reaction(BackendReaction(
        stream_id=3,
        stream_kind='group',
        stream_external_id='629201002',
        target_external_message_id='9312',
        reaction='赞',
        turn_id=1275,
    ))

    assert len(backend.failures) == 1
    assert backend.failures[0]['action'] == 'react'
    assert backend.failures[0]['target'] == '9312'


@pytest.mark.asyncio
async def test_report_failure_does_not_break_outbound_loop() -> None:
    """回报本身失败只记日志：否则一次局部失败会升级成出站通道停摆。"""

    class _BrokenBackend:
        async def report_delivery_failure(self, **kwargs: Any) -> None:
            raise RuntimeError('主体不可达')

    runner = OneBot11Runner(
        _document(),
        backend_port=1,
        token='backend-secret',
        transport=_FailingTransport(_PACKET_BACKEND_FAILURE),
        backend=_BrokenBackend(),
    )

    # 不抛出即通过：回报失败必须被吞掉并记日志。
    await runner._apply_poke(BackendPoke(
        stream_id=3,
        stream_kind='group',
        stream_external_id='629201002',
        target_external_id='3209184542',
        turn_id=1275,
    ))
