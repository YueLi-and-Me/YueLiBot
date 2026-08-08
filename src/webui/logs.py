"""WebUI 实时日志的进程内有界广播。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Set, Tuple

import asyncio
import threading


@dataclass(eq=False)
class _Subscriber:
    """一个 WebSocket 连接对应的事件循环与有界队列。"""

    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Dict[str, object]]


class WebUiLogStream:
    """保存最近日志，并把新行安全地投递到各 WebSocket 所在线程。"""

    def __init__(self, backlog_size: int = 300, queue_size: int = 200) -> None:
        self._backlog: Deque[Dict[str, object]] = deque(maxlen=backlog_size)
        self._queue_size = queue_size
        self._subscribers: Set[_Subscriber] = set()
        self._seq = 0
        self._lock = threading.Lock()

    def publish(self, line: str) -> Dict[str, object]:
        """登记一行已经按控制台规则渲染、且始终带 ANSI 颜色的日志。"""
        with self._lock:
            self._seq += 1
            item: Dict[str, object] = {'seq': self._seq, 'line': line}
            self._backlog.append(item)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.loop.call_soon_threadsafe(self._offer, subscriber.queue, item)
        return item

    def subscribe(self) -> Tuple[_Subscriber, List[Dict[str, object]]]:
        """订阅后返回当前积压副本，避免注册与读历史之间漏行。"""
        subscriber = _Subscriber(
            loop=asyncio.get_running_loop(),
            queue=asyncio.Queue(maxsize=self._queue_size),
        )
        with self._lock:
            backlog = list(self._backlog)
            self._subscribers.add(subscriber)
        return subscriber, backlog

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    @staticmethod
    def _offer(
        queue: asyncio.Queue[Dict[str, object]],
        item: Dict[str, object],
    ) -> None:
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(item)

    def clear(self) -> None:
        """仅供隔离测试清空进程级缓冲。"""
        with self._lock:
            self._backlog.clear()
            self._subscribers.clear()
            self._seq = 0


webui_logs = WebUiLogStream()
