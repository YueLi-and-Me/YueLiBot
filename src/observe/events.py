"""管线事件的唯一出口：落账、广播、更新阶段板并写调试日志。"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Set

import asyncio
import threading

from src.common.clock import now as current_time
from src.common.logger import get_logger
from src.observe.board import board
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
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Dict[str, Any]]
    overflowed: asyncio.Event


class EventBroadcaster:
    """进程内事件广播。"""

    def __init__(self, queue_size: int = 200) -> None:
        if queue_size < 1:
            raise ValueError("事件广播队列容量必须大于 0")
        self._queue_size = queue_size
        self._subscribers: Set[EventSubscriber] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> EventSubscriber:
        subscriber = EventSubscriber(
            loop=asyncio.get_running_loop(),
            queue=asyncio.Queue(maxsize=self._queue_size),
            overflowed=asyncio.Event(),
        )
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: EventSubscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.loop.call_soon_threadsafe(self._offer, subscriber, entry)

    @staticmethod
    def _offer(subscriber: EventSubscriber, entry: Dict[str, Any]) -> None:
        if subscriber.overflowed.is_set():
            return
        if subscriber.queue.full():
            subscriber.overflowed.set()
            return
        subscriber.queue.put_nowait(entry)

    def clear(self) -> None:
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
    """绑定本轮来源。"""
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
    return _current_stage.get()


def current_stream_id() -> int | None:
    return _current_stream_id.get()


def current_turn_id() -> int | None:
    return _current_turn_id.get()


def emit(event_kind: str, **fields: Any) -> Dict[str, Any]:
    """登记事件。"""
    merged = {**_origin.get(), **fields}
    stream_id = merged.pop("streamId", _current_stream_id.get())
    turn_id = merged.pop("turnId", _current_turn_id.get())
    for reserved in _RESERVED_FIELDS:
        merged.pop(reserved, None)
    stage = _current_stage.get()

    if event_kind in LIVE_ONLY_KINDS or not event_store.configured:
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
    """切换并登记阶段。"""
    _current_stage.set(stage.id)
    _current_stream_id.set(stream_id)
    _current_turn_id.set(turn_id)
    board._update(stream_id, stream_name, stage, detail, turn_id)
    return emit(
        "stage",
        streamId=stream_id,
        turnId=turn_id,
        streamName=stream_name,
        detail=detail,
    )


def reset_for_tests() -> None:
    _origin.set({})
    _current_stage.set("")
    _current_stream_id.set(None)
    _current_turn_id.set(None)
    board.clear()
    broadcaster.clear()
