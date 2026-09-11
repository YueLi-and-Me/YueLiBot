"""活动与睡眠生命周期脱离桌宠开关的接线测试。"""

from __future__ import annotations

from typing import Any, List

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config.schema import Config
from src.core.runtime.clock import now as current_time
from src.core.schedule.timeline import ActivityDraft, ActivityTimeline, ActivityTransition
from src.core.services.chat import ChatService
from src.core.services.proactive import AwarenessService


async def _noop_push(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    return None


def _insert_expired_activity(db: sqlite3.Connection, now: int) -> int:
    """写入一段早已过期但仍进行中的清醒活动，等待后台决策推进。"""

    cursor = db.execute(
        """INSERT INTO activities
             (kind, doing, mood, energy_pace, mood_pace, advances,
              started_at, expected_until, ended_at, source)
           VALUES ('awake', '写代码', '状态平稳', 0, 0, NULL, ?, ?, NULL, 'decided')""",
        (now - 2 * 60 * 60_000, now - 60 * 60_000),
    )
    db.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


@pytest.mark.parametrize('proactive_enabled', [True, False])
async def test_poll_advances_timeline_when_desktop_pet_disabled(
    db: sqlite3.Connection, proactive_enabled: bool,
) -> None:
    """桌宠关闭、无任何对话时，轮询仍推进活动时间线且不产生主动发言。"""

    now = current_time()
    expired_id = _insert_expired_activity(db, now)
    calls: List[tuple[int, int]] = []

    async def decider(_activity, _now: int, gap_ms: int) -> ActivityTransition:
        calls.append((expired_id, gap_ms))
        return ActivityTransition(next_activity=ActivityDraft(
            kind='rest',
            doing='靠在椅子上闭目养神',
            mood='放松但仍然清醒',
            energy_pace=1,
            mood_pace=0,
            minutes=30,
        ))

    timeline = ActivityTimeline(db)
    timeline.set_decider(decider)
    cfg = Config()
    cfg.desktop_pet.enabled = False
    cfg.generation.proactive.enabled = proactive_enabled
    chat = ChatService(db, None, None, None, _noop_push, cfg=cfg)
    chat._chat_provider = object()
    chat.compose_proactive = AsyncMock(return_value=[{'text': '桌面主动发言'}])
    chat.speak_claimed = MagicMock(return_value=1)

    service = AwarenessService(
        chat=chat,
        schedule=None,
        timeline=timeline,
        cfg=cfg,
        push_event=_noop_push,
        sensor=None,
    )
    assert service._enabled is False

    # startup 必须创建 task 后立即返回；若把轮询本身 await 在 startup 里，这里会超时。
    await asyncio.wait_for(service.startup(), timeout=1.0)
    try:
        assert service._poll_task is not None
        for _ in range(100):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls, '桌宠关闭时轮询没有推进活动时间线'
        row = db.execute(
            "SELECT kind FROM activities WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None and row['kind'] == 'rest'
        chat.compose_proactive.assert_not_awaited()
        chat.speak_claimed.assert_not_called()
    finally:
        await service.shutdown()
