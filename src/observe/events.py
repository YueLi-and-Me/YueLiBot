"""提供管线事件的统一登记、持久化、广播和上下文绑定入口。

持久化事件写入 `EventStore`，高频实时事件只广播不落账；事件同时携带当前
stream、turn 和阶段信息，供 WebUI 与日志追踪使用。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Set

import asyncio
import threading

from src.common.clock import now as current_time
from src.common.logger import get_logger
from src.observe.stages import Stage, label_for
from src.observe.store import event_store


logger = get_logger(__name__)

LIVE_ONLY_KINDS = frozenset({"llm_chunk", "foreground"})
_RESERVED_FIELDS = frozenset({"seq", "at", "kind", "stage", "stageLabel", "streamId", "turnId"})

_origin: ContextVar[Dict[str, Any]] = ContextVar("event_origin", default={})
_current_stage: ContextVar[str] = ContextVar("pipeline_stage", default="")
_current_stream_id: ContextVar[int | None] = ContextVar("pipeline_stream_id", default=None)
_current_turn_id: ContextVar[int | None] = ContextVar("pipeline_turn_id", default=None)


@dataclass(eq=False)
class EventSubscriber:
    """表示一个事件广播订阅者及其异步接收资源。

    :ivar loop: 订阅者所属的 asyncio 事件循环。
    :ivar queue: 接收事件字典的有界队列。
    :ivar overflowed: 队列溢出时被置位的事件标志。
    """

    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Dict[str, Any]]
    overflowed: asyncio.Event


class EventBroadcaster:
    """在线程安全的订阅者集合中广播进程内事件。

    发布者可以来自非 asyncio 线程；实现使用每个订阅者的事件循环投递回调。
    """

    def __init__(self, queue_size: int = 200) -> None:
        """创建有界事件广播器。

        :param queue_size: 每个订阅者队列容量，默认值为 200，必须大于 0。
        :raises ValueError: `queue_size` 小于 1。
        :side_effects: 初始化订阅者集合、锁和队列容量。
        """
        if queue_size < 1:
            raise ValueError("事件广播队列容量必须大于 0")
        self._queue_size = queue_size
        self._subscribers: Set[EventSubscriber] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> EventSubscriber:
        """为当前运行事件循环创建并登记一个订阅者。

        :return: 新的订阅者对象。
        :raises RuntimeError: 当前线程没有运行中的 asyncio 事件循环。
        :side_effects: 修改订阅者集合。
        """
        subscriber = EventSubscriber(
            loop=asyncio.get_running_loop(),
            queue=asyncio.Queue(maxsize=self._queue_size),
            overflowed=asyncio.Event(),
        )
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        """移除订阅者，重复移除安全。

        :param subscriber: 要移除的订阅者。
        :side_effects: 修改订阅者集合。
        """
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, entry: Dict[str, Any]) -> None:
        """向当前所有订阅者异步投递一条事件。

        :param entry: 可 JSON 序列化的事件字典。
        :side_effects: 在线程锁外向每个订阅者的事件循环安排投递回调；队列满时标记溢出。
        :performance: 按订阅者数量线性安排回调，不等待网络发送。
        """
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.loop.call_soon_threadsafe(self._offer, subscriber, entry)

    @staticmethod
    def _offer(subscriber: EventSubscriber, entry: Dict[str, Any]) -> None:
        """把事件放入订阅者队列，队列满时标记溢出。

        :param subscriber: 目标订阅者。
        :param entry: 待投递事件。
        :side_effects: 可能设置 `overflowed` 或向有界队列写入事件。
        """
        if subscriber.overflowed.is_set():
            return
        if subscriber.queue.full():
            subscriber.overflowed.set()
            return
        subscriber.queue.put_nowait(entry)

    def clear(self) -> None:
        """清空订阅者集合，不主动关闭各 WebSocket。

        :side_effects: 删除所有订阅记录；已安排的事件循环回调仍可能执行。
        """
        with self._lock:
            self._subscribers.clear()


broadcaster = EventBroadcaster()


def bind_origin(
    stream_id: int,
    platform: str,
    person_id: int,
    person_kind: str,
    sender_external_id: str,
    sender_nickname: str,
    sender_group_card: str,
    sender_display_name: str,
    sender_label: str,
    bot_name: str,
) -> None:
    """将当前异步上下文绑定到本轮消息来源。

    Args:
        stream_id: 当前 stream 数据库 ID。
        platform: 消息来源平台标识。
        person_id: 当前发送者人物 ID。
        person_kind: 人物类型，例如 ``owner`` 或 ``contact``。
        sender_external_id: 平台侧发送者外部 ID。
        sender_nickname: 平台侧账号昵称。
        sender_group_card: 当前群聊中的群名片；非群聊为空字符串。
        sender_display_name: 当前会话最终使用的显示名。
        sender_label: 观测面板展示的发送者标签。
        bot_name: 当前平台使用的 Bot 名称。

    Side Effects:
        覆盖当前异步上下文的来源字段；不写入事件账本或广播事件。
    """
    _origin.set({
        "streamId": stream_id,
        "platform": platform,
        "personId": person_id,
        "personKind": person_kind,
        "senderExternalId": sender_external_id,
        "senderNickname": sender_nickname,
        "senderGroupCard": sender_group_card,
        "senderDisplayName": sender_display_name,
        "senderLabel": sender_label,
        "botName": bot_name,
    })


def current_stage_id() -> str:
    """返回当前异步上下文绑定的阶段 ID。

    :return: 阶段 ID；未绑定时为空字符串。
    :side_effects: 不修改上下文。
    """
    return _current_stage.get()


def current_stream_id() -> int | None:
    """返回当前异步上下文绑定的 stream ID。

    :return: stream ID；未绑定时为 `None`。
    :side_effects: 不修改上下文。
    """
    return _current_stream_id.get()


def current_turn_id() -> int | None:
    """返回当前异步上下文绑定的聊天轮次 ID。

    :return: turn ID；未绑定时为 `None`。
    :side_effects: 不修改上下文。
    """
    return _current_turn_id.get()


def emit(event_kind: str, **fields: Any) -> Dict[str, Any]:
    """登记、持久化并广播一条管线事件。

    Args:
        event_kind: 事件类型名称；属于 ``LIVE_ONLY_KINDS`` 时只实时广播，否则写入事件账本。
        **fields: 事件附加字段；保留字段由当前上下文或函数参数统一管理。

    Returns:
        已补充序号、时间、阶段、stream 和 turn 字段的事件字典；实时事件的序号为 ``None``。

    Raises:
        sqlite3.Error: 持久化事件写入失败。
        TypeError: 字段无法被事件存储或广播逻辑处理时抛出。

    Side Effects:
        可能写入事件账本，向全部订阅者队列投递事件，并记录调试日志。
    """
    merged = {**_origin.get(), **fields}
    stream_id = merged.pop("streamId", _current_stream_id.get())
    turn_id = merged.pop("turnId", _current_turn_id.get())
    for reserved in _RESERVED_FIELDS:
        merged.pop(reserved, None)
    stage = _current_stage.get()

    if event_kind in LIVE_ONLY_KINDS:
        entry: Dict[str, Any] = {
            "seq": None,
            "at": current_time(),
            "kind": event_kind,
            "stage": stage,
            "stageLabel": label_for(stage),
            "streamId": stream_id,
            "turnId": turn_id,
            **merged,
        }
    else:
        entry = event_store.append(event_kind, stage, stream_id, turn_id, merged)

    broadcaster.publish(entry)
    logger.debug("trace", **entry)
    return entry


def enter_stage(
    stage: Stage,
    stream_id: int,
    stream_name: str,
    detail: str = "",
    turn_id: int | None = None,
) -> Dict[str, Any]:
    """更新当前管线阶段和事件上下文，并登记阶段事件。

    Args:
        stage: 目标阶段定义。
        stream_id: 目标 stream 数据库 ID。
        stream_name: 观察面板使用的 stream 可读名称。
        detail: 阶段附加说明，默认为空字符串。
        turn_id: 可选聊天轮次 ID；无轮次时为 ``None``。

    Returns:
        已写入并广播的阶段事件字典。

    Raises:
        sqlite3.Error: 阶段事件持久化失败。

    Side Effects:
        更新 ContextVar，随后调用 ``emit`` 写入并广播阶段事件。
    """
    _current_stage.set(stage.id)
    _current_stream_id.set(stream_id)
    _current_turn_id.set(turn_id)
    return emit(
        "stage",
        streamId=stream_id,
        turnId=turn_id,
        streamName=stream_name,
        detail=detail,
    )


def reset_for_tests() -> None:
    """清除当前事件上下文和广播订阅者。

    :side_effects: 重置 ContextVar 并清空全局 broadcaster；仅供测试隔离使用，
        不删除事件账本中的历史记录。
    """
    _origin.set({})
    _current_stage.set("")
    _current_stream_id.set(None)
    _current_turn_id.set(None)
    broadcaster.clear()
