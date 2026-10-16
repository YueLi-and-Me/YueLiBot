"""回合面板在真实回合链路上的可见性验收。

盯住两件事：查询类工具的完整结果进入回合面板；沉默这类不出回复面板的回合
仍把已发生的决策与工具结果补成面板。着色裁定是进程级状态，可能已被同进程里
先跑的用例改写，这里只比对去色后的文本；不着色时的纯文本输出由面板单测覆盖。
"""

from __future__ import annotations

from typing import Any, List

import re
import sqlite3

from src.core.config.schema import Config
from src.core.platform_io.types import DeliveryReceipt, InboundMessage
from src.core.services.chat import ChatService

_ANSI = re.compile(r'\x1b\[[0-9;]*m')


class _ChatProvider:
    """按脚本逐轮产出 XML 动作的替身聊天模型。"""

    def __init__(self, scripts: List[List[str]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, messages=None, **_kwargs: Any):
        index = self.calls
        self.calls += 1
        for text in self.scripts[min(index, len(self.scripts) - 1)]:
            yield {'text': text}


class _Broker:
    def __init__(self) -> None:
        self.dispatched: List[Any] = []

    async def dispatch(self, message: Any) -> Any:
        self.dispatched.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=['fake-1'],
        )


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


def _chat(db: sqlite3.Connection, scripts: List[List[str]]) -> ChatService:
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'enabled'
    config.conversation_agent.max_cognitive_rounds = 2
    config.group_chat.scene_refresh_messages = 0
    return ChatService(
        db, _ChatProvider(scripts), None, None, _noop, cfg=config, broker=_Broker(),
    )


async def test_reply_turn_panel_contains_tool_result(db: sqlite3.Connection, capsys) -> None:
    """查过记忆再回复的回合，面板里有该次检索的入参与结果，且为纯文本。"""
    chat = _chat(db, [
        ['<decision action="recall" query="龙族"/>'],
        [
            '<decision action="reply" targets="1" reasons="pending_thread" length="brief"/>',
            '<say emotion="smile">没查到。</say>',
        ],
    ])
    desktop = chat.desktop_context
    await chat.send(InboundMessage(text='你记得龙族吗', context=desktop))
    await chat._tick()
    await chat._inflight[desktop.stream.id].task

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '工具结果 · 翻长期记忆（recall）' in out
    assert '入参：龙族' in out
    assert '关于「龙族」' in out or '和「龙族」' in out


async def test_silent_turn_still_renders_panel(db: sqlite3.Connection, capsys) -> None:
    """查完决定沉默的回合不出回复面板，但查询结果要以「未发言」面板出现。"""
    chat = _chat(db, [
        ['<decision action="recall" query="龙族"/>'],
        ['<decision action="silent" reasons="no_new_value"/>'],
    ])
    context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='86420',
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='小李',
        first_seen_at=1_000_000,
    )
    await chat.send(InboundMessage(text='月璃知道龙族吗', context=context, name_mentioned=True))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '· 未发言' in out
    assert '工具结果 · 翻长期记忆（recall）' in out
    assert '入参：龙族' in out
