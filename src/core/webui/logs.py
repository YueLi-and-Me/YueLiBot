"""在进程内缓存并广播 WebUI 实时日志。

日志发布线程与 WebSocket 事件循环可能不同，本模块用线程锁保护订阅表，并通过
``call_soon_threadsafe`` 将新日志投递到订阅者自己的 asyncio 队列；积压和单订阅
队列均设置上限，防止客户端断开或处理缓慢时无限占用内存。
"""

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
    """保存有限积压日志并跨线程广播到 WebSocket 订阅者。"""

    def __init__(self, backlog_size: int = 300, queue_size: int = 200) -> None:
        """初始化日志积压和订阅队列。

        :param backlog_size: 内存中保留的最近日志条数，默认 300。
        :param queue_size: 每个订阅者队列容量，默认 200；满队列丢弃最旧事件。

        :raises ValueError: ``deque`` 或 ``asyncio.Queue`` 对非法容量的错误由标准库
                直接传播。
        """

        self._backlog: Deque[Dict[str, object]] = deque(maxlen=backlog_size)
        self._queue_size = queue_size
        self._subscribers: Set[_Subscriber] = set()
        self._seq = 0
        self._lock = threading.Lock()

    def publish(self, line: str) -> Dict[str, object]:
        """登记一行日志并向当前订阅者异步广播。

        :param line: 已按控制台规则渲染的日志文本，通常包含 ANSI 颜色控制码。

        :return: 包含单调递增 ``seq`` 和 ``line`` 字段的日志事件字典。

        副作用：
            更新有限 backlog 和序号，并在线程锁外向每个订阅者事件循环安排投递回调。
            积压和队列容量由构造参数限制。
        """
        with self._lock:
            self._seq += 1
            item: Dict[str, object] = {'seq': self._seq, 'line': line}
            self._backlog.append(item)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.loop.call_soon_threadsafe(self._offer, subscriber.queue, item)
        return item

    def subscribe(self) -> Tuple[_Subscriber, List[Dict[str, object]]]:
        """登记当前事件循环的日志订阅者并返回注册时的积压副本。

        :return: ``(订阅者, backlog)``；backlog 是注册时内存积压的独立列表副本。

        :raises RuntimeError: 当前线程没有运行中的 asyncio 事件循环。

        副作用：
            在线程锁保护下添加订阅者；先复制积压再登记订阅，避免注册与读取之间漏行。
        """
        subscriber = _Subscriber(
            loop=asyncio.get_running_loop(),
            queue=asyncio.Queue(maxsize=self._queue_size),
        )
        with self._lock:
            backlog = list(self._backlog)
            self._subscribers.add(subscriber)
        return subscriber, backlog

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        """移除一个日志订阅者。

        :param subscriber: ``subscribe`` 返回的订阅对象；重复移除安全。

        副作用：
            在线程锁保护下从订阅集合删除对象，不清理已经投递到其队列的事件。
        """

        with self._lock:
            self._subscribers.discard(subscriber)

    @staticmethod
    def _offer(
        queue: asyncio.Queue[Dict[str, object]],
        item: Dict[str, object],
    ) -> None:
        """向订阅队列写入一条日志并在满队列时丢弃最旧项。

        :param queue: 目标 asyncio 队列。
        :param item: 待投递的日志字典。

        副作用：
            可能移除队列头部一条旧日志，再追加当前日志；调用发生在目标事件循环。
        """

        if queue.full():
            queue.get_nowait()
        queue.put_nowait(item)

    def clear(self) -> None:
        """清空进程级日志积压、订阅者和序号。

        副作用：
            删除内存中的 backlog 和订阅集合，将序号重置为 ``0``；不发送关闭事件，
            仅供测试隔离使用。
        """
        with self._lock:
            self._backlog.clear()
            self._subscribers.clear()
            self._seq = 0


webui_logs = WebUiLogStream()
