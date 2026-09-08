"""名单外会话的拒绝日志去重回归。

覆盖 `OneBot11Runner._consume_protocol_events` 对 `private_denied` /
`group_denied` 的记录口径：访问名单是配置事实，同一个会话首次记 info，
其后降为 debug；名单外群聊的通知事件也归入拒绝，不再落「忽略未处理的 QQ 事件」。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Tuple

from src.platforms.onebot11.config import (
    AdapterDocument,
    GroupAccessConfig,
    PrivateAccessConfig,
    ProtocolConnectionConfig,
)
from src.platforms.onebot11.runner import OneBot11Runner
import src.platforms.onebot11.runner as runner_module


def _document() -> AdapterDocument:
    """构造只放行单个群、私聊白名单为空的最小配置。"""

    return AdapterDocument(
        inner={'version': '0.1.0'},
        napcat=ProtocolConnectionConfig(
            enabled=True,
            self_qq='13579',
            host='127.0.0.1',
            port=8095,
            token='',
            reconnect_interval_sec=5,
            action_timeout_sec=15,
        ),
        owner={'qq': '24680'},
        private=PrivateAccessConfig(),
        group=GroupAccessConfig(mode='whitelist', list=['629201002']),
    )


def _group_message(message_id: int, group_id: int) -> Dict[str, Any]:
    """构造一条群文字消息。"""

    return {
        'post_type': 'message',
        'message_type': 'group',
        'message_id': message_id,
        'group_id': group_id,
        'user_id': 97531,
        'self_id': 13579,
        'message': [{'type': 'text', 'data': {'text': f'第 {message_id} 条'}}],
        'sender': {'user_id': 97531, 'nickname': '群友'},
    }


def _private_message(message_id: int, user_id: int) -> Dict[str, Any]:
    """构造一条名单外私聊文字消息。"""

    return {
        'post_type': 'message',
        'message_type': 'private',
        'message_id': message_id,
        'user_id': user_id,
        'self_id': 13579,
        'message': [{'type': 'text', 'data': {'text': '陌生人私聊'}}],
        'sender': {'user_id': user_id, 'nickname': '陌生人'},
    }


def _group_recall(group_id: int) -> Dict[str, Any]:
    """构造一条群撤回通知，代表适配器不处理的通知类型。"""

    return {
        'post_type': 'notice',
        'notice_type': 'group_recall',
        'group_id': group_id,
        'user_id': 97531,
        'operator_id': 97531,
        'message_id': 900,
        'self_id': 13579,
    }


class _ReplayTransport:
    """按序回放预置事件，不提供任何 action 能力。"""

    def __init__(self, payloads: List[Dict[str, Any]]) -> None:
        self._payloads = payloads

    async def iter_events(self) -> AsyncIterator[Dict[str, Any]]:
        for payload in self._payloads:
            yield payload


class _RejectingBackend:
    """拒绝的事件不该走到主体提交，任何调用都视为回归失败。"""

    async def submit_inbound(self, event: object) -> None:
        raise AssertionError('名单外会话不应提交入站事件')


class _LogRecorder:
    """按级别记录日志事件名与结构化字段。"""

    def __init__(self) -> None:
        self.records: List[Tuple[str, str, Dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.records.append(('info', event, fields))

    def debug(self, event: str, **fields: Any) -> None:
        self.records.append(('debug', event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self.records.append(('warning', event, fields))

    def error(self, event: str, **fields: Any) -> None:
        self.records.append(('error', event, fields))


def _events(logs: _LogRecorder, level: str) -> List[str]:
    """取出指定级别下的日志事件名序列。"""

    return [event for record_level, event, _ in logs.records if record_level == level]


async def _consume(payloads: List[Dict[str, Any]], monkeypatch) -> _LogRecorder:
    """用替身 transport 与后端跑完一轮事件消费，返回日志记录器。"""

    logs = _LogRecorder()
    monkeypatch.setattr(runner_module, 'logger', logs)
    runner = OneBot11Runner(
        _document(),
        backend_port=1,
        token='backend-secret',
        transport=_ReplayTransport(payloads),
        backend=_RejectingBackend(),
    )
    await runner._consume_protocol_events('13579', '月璃')
    return logs


async def test_group_denied_logs_once_per_group(monkeypatch) -> None:
    """活跃的名单外群每条消息记一行会刷屏，同群只留首次 info。"""

    logs = await _consume(
        [
            _group_message(1, 823608931),
            _group_message(2, 823608931),
            _group_message(3, 823608931),
            _group_message(4, 958605377),
        ],
        monkeypatch,
    )

    assert _events(logs, 'info') == ['QQ 群聊访问被拒', 'QQ 群聊访问被拒']
    assert [fields['groupId'] for level, _, fields in logs.records if level == 'info'] == [
        823608931, 958605377,
    ]
    assert _events(logs, 'debug') == ['QQ 群聊访问被拒'] * 2


async def test_denied_group_notice_folds_into_access_denied(monkeypatch) -> None:
    """名单外群的通知归入拒绝：否则撤回等事件会绕开去重继续刷屏。"""

    logs = await _consume(
        [_group_message(1, 823608931), _group_recall(823608931)],
        monkeypatch,
    )

    assert _events(logs, 'info') == ['QQ 群聊访问被拒']
    assert _events(logs, 'debug') == ['QQ 群聊访问被拒']
    assert '忽略未处理的 QQ 事件' not in _events(logs, 'info')


async def test_private_and_group_denials_dedupe_independently(monkeypatch) -> None:
    """群号与 QQ 号可能是同一串数字，去重键必须分命名空间。"""

    logs = await _consume(
        [
            _group_message(1, 823608931),
            _private_message(2, 823608931),
            _private_message(3, 823608931),
        ],
        monkeypatch,
    )

    assert _events(logs, 'info') == ['QQ 群聊访问被拒', 'QQ 私聊访问被拒']
    assert _events(logs, 'debug') == ['QQ 私聊访问被拒']
