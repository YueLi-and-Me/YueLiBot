"""维护每条 stream 当前所处的管线阶段及阶段耗时。

`StageBoard` 只保存最新阶段快照，不承担事件持久化；事件账本由
`src.observe.store` 负责，阶段切换通过 `src.observe.events.enter_stage` 统一触发。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import threading

from src.common.clock import now as current_time
from src.observe.stages import Stage


@dataclass
class _Entry:
    """保存一条 stream 的当前阶段快照。

    :ivar stream_id: stream 数据库 ID。
    :ivar stream_name: 面向观察面板的 stream 名称。
    :ivar stage: 当前阶段定义。
    :ivar detail: 当前阶段附加说明。
    :ivar turn_id: 关联聊天轮次 ID，可以为 `None`。
    :ivar stage_started_at: 当前阶段开始时间戳。
    :ivar updated_at: 最近更新时间戳。
    """

    stream_id: int
    stream_name: str
    stage: Stage
    detail: str
    turn_id: int | None
    stage_started_at: int
    updated_at: int


class StageBoard:
    """线程安全地维护各 stream 的当前阶段快照。"""

    def __init__(self) -> None:
        """创建空的阶段板。

        :side_effects: 初始化线程锁和 stream 快照字典。
        """
        self._lock = threading.Lock()
        self._entries: Dict[int, _Entry] = {}

    def _update(
        self,
        stream_id: int,
        stream_name: str,
        stage: Stage,
        detail: str = "",
        turn_id: int | None = None,
    ) -> None:
        """更新一个 stream 的阶段和阶段时间。

        :param stream_id: 目标 stream ID。
        :param stream_name: 面板显示名称。
        :param stage: 新阶段。
        :param detail: 阶段详情，默认值为空字符串。
        :param turn_id: 关联轮次 ID，默认值为 `None`。
        :side_effects: 在锁保护下写入快照；阶段未改变时保留原开始时间。
        """
        now = current_time()
        with self._lock:
            current = self._entries.get(stream_id)
            started_at = (
                now
                if current is None or current.stage.id != stage.id
                else current.stage_started_at
            )
            self._entries[stream_id] = _Entry(
                stream_id=stream_id,
                stream_name=stream_name,
                stage=stage,
                detail=detail,
                turn_id=turn_id,
                stage_started_at=started_at,
                updated_at=now,
            )

    def snapshot(self) -> List[dict]:
        """返回按最近更新时间倒序排列的所有阶段快照。

        :return: 可 JSON 序列化的阶段字典列表，包含阶段耗时和更新时间。
        :side_effects: 只读取锁保护的快照副本，不修改内部状态。
        """
        now = current_time()
        with self._lock:
            entries = list(self._entries.values())
        entries.sort(key=lambda entry: entry.updated_at, reverse=True)
        return [
            {
                "streamId": entry.stream_id,
                "streamName": entry.stream_name,
                "stage": entry.stage.id,
                "stageLabel": entry.stage.label,
                "detail": entry.detail,
                "turnId": entry.turn_id,
                "stageElapsedMs": now - entry.stage_started_at,
                "updatedAt": entry.updated_at,
            }
            for entry in entries
        ]

    def clear(self) -> None:
        """清空所有 stream 阶段快照。

        :side_effects: 在锁保护下删除内部快照。
        """
        with self._lock:
            self._entries.clear()


board = StageBoard()
