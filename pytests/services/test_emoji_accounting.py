"""表情包使用记账的可见性回归：落账告警、轮末面板行与容量口径。

use_count 是一个没有明细的聚合数：record_use 未命中时此前完全静默，轮末面板
也没有表情包分支，库在 2026-08-25 前的发送又天然记 0。本文件盯住三件事：
发送成功后每张表情包要么落账、要么留下可定位的警告；面板对「选中」与
「落空」两种情形各渲染一行；容量判定不把封禁行算进去。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

import asyncio
import hashlib
import re
import sqlite3

from PIL import Image
from structlog.testing import capture_logs

import pytest

import src.core.observe.events as events_module
import src.core.services.console.trace_console as console
from src.core.agent.parser import EmojiEvent
from src.core.config.schema import Config
from src.core.observe import events as trace
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import DeliveryReceipt
from src.core.services.chat import ChatService
from src.core.services.chat.outbound import _send_ref_content_hash
from src.core.services.chat.state import _TurnSink
from src.core.services.media.emoji import EmojiLibrary
from src.main import _emoji_capacity_exceeded

_ANSI = re.compile(r'\x1b\[[0-9;]*m')


def _png_bytes(color: str = 'red') -> bytes:
    """生成可被 Pillow 完整校验的小型 PNG 测试素材。

    需要互不相同的素材时必须挑**灰度差异明显**的颜色：视觉身份按 32×32 灰度
    缩略图比对，PIL 的 red(255,0,0) 与 green(0,128,0) 灰度分别是 76 与 75，
    会被判为同一张图而合并成一条记录。
    """

    stream = BytesIO()
    Image.new('RGB', (2, 2), color=color).save(stream, format='PNG')
    return stream.getvalue()


async def _noop(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    return None


class _RecordingBroker:
    """记录出站载荷并直接返回成功回执的平台替身。"""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def dispatch(self, message: Any) -> DeliveryReceipt:
        self.messages.append(message)
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=[],
        )


def _qq_group_context(db: sqlite3.Connection) -> Any:
    registry = StreamRegistry(db)
    return registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='group-7',
        sender_external_id='contact-42',
        sender_nickname='账号昵称',
        sender_group_card='小李',
        first_seen_at=1_700_000_000_000,
    )


def _chat(db: sqlite3.Connection, library: EmojiLibrary) -> ChatService:
    return ChatService(
        db, None, None, None, _noop,
        cfg=Config(),
        emoji_library=library,
        broker=_RecordingBroker(),
    )


def _sink(context: Any) -> _TurnSink:
    return _TurnSink(
        context=context,
        cancel_event=asyncio.Event(),
        turn=9,
        now=1_700_000_100_000,
        source_text='发个表情',
    )


@pytest.fixture
def spy_trace(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """记录全部观察事件，同时保留原始 emit 的落账与广播。"""
    recorded: list[dict] = []
    original = events_module.emit

    def _record(kind: str, **fields: Any) -> dict:
        recorded.append({'kind': kind, **fields})
        return original(kind, **fields)

    monkeypatch.setattr(events_module, 'emit', _record)
    return recorded


@pytest.fixture
def rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制打开面板渲染；pytest 下默认关闭，否则渲染函数直接是空操作。"""
    monkeypatch.setattr(console, '_render_enabled', True)


async def test_record_use_miss_logs_warning(db: sqlite3.Connection, tmp_path: Path) -> None:
    """record_use 未命中行时必须留下带 send_ref 与哈希前缀的警告，不许静默。"""
    library = EmojiLibrary(db, tmp_path / 'emojis')
    chat = _chat(db, library)
    context = _qq_group_context(db)
    bogus_ref = (tmp_path / 'emojis' / ('0' * 64 + '.png')).as_uri()

    with capture_logs() as logs:
        await chat._dispatch_outbound(context, 5, ['哈哈'], [('开心', bogus_ref, 2)])

    warnings = [entry for entry in logs if entry.get('event') == 'emoji_use_record_missed']
    assert len(warnings) == 1
    assert warnings[0]['sendRef'] == bogus_ref
    assert warnings[0]['hash'] == '00000000'
    assert warnings[0]['turnId'] == 5


async def test_record_use_hit_emits_trace_event(
    db: sqlite3.Connection, tmp_path: Path, spy_trace: list[dict],
) -> None:
    """record_use 命中时发出带哈希前缀与目标情绪的观察事件，使用计数加一。"""
    library = EmojiLibrary(db, tmp_path / 'emojis')
    image = _png_bytes()
    send_ref = await library.register(image, '开心,可爱', 'image/png')
    chat = _chat(db, library)
    context = _qq_group_context(db)

    await chat._dispatch_outbound(context, 6, ['哈哈'], [('开心', send_ref, 2)])

    recorded = [entry for entry in spy_trace if entry['kind'] == 'emoji_use_recorded']
    assert len(recorded) == 1
    assert recorded[0]['hash'] == _send_ref_content_hash(send_ref)[:8]
    assert recorded[0]['emotion'] == '开心'
    assert recorded[0]['turnId'] == 6
    row = db.execute(
        'SELECT use_count FROM emoji WHERE hash = ?',
        (hashlib.sha256(image).hexdigest(),),
    ).fetchone()
    assert row[0] == 1


def test_selected_emoji_renders_in_turn_panel(rendering, capsys: pytest.CaptureFixture) -> None:
    """命中情形的面板行：哈希前 8 位、目标情绪与库内标签都在轮末面板上。"""
    console.mark_turn_start(21)
    console.render_turn(
        21, '小李', '发个表情', messages=[], reply_segments=['哈哈'],
        side_effects=[{
            'kind': 'emoji_selected',
            'hash': '723b3877',
            'emotion': '开心',
            'tags': '开心,可爱',
            'candidateCount': 1,
        }],
        bot_name='月璃', source_label='群聊·629201002',
    )

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '表情包：723b3877 · 目标情绪 开心 · 标签 开心,可爱 · 候选 1 张' in out


def test_missed_emoji_renders_in_turn_panel(rendering, capsys: pytest.CaptureFixture) -> None:
    """落空情形的面板行：写明写了 <emoji> 但库里没命中，不让落空继续隐身。"""
    console.mark_turn_start(22)
    console.render_turn(
        22, '小李', '发个表情', messages=[], reply_segments=['哈哈'],
        side_effects=[{'kind': 'emoji_selection_missed', 'emotion': '量子涨落'}],
        bot_name='月璃', source_label='群聊·629201002',
    )

    out = _ANSI.sub('', capsys.readouterr().out)
    assert '表情包落空：目标情绪 量子涨落，写了 <emoji> 但库里没命中' in out


async def test_consume_events_stores_selected_side_effect(
    db: sqlite3.Connection, tmp_path: Path, spy_trace: list[dict],
) -> None:
    """命中时 sink 同步拿到出站引用与面板副作用，字段足以指认选中了哪张。"""
    library = EmojiLibrary(db, tmp_path / 'emojis')
    send_ref = await library.register(_png_bytes(), '开心,可爱', 'image/png')
    chat = _chat(db, library)
    context = _qq_group_context(db)
    sink = _sink(context)

    await chat._consume_events([EmojiEvent(emotion='开心')], sink)

    assert sink.emoji_items == [('开心', send_ref, 1)]
    assert sink.side_effects == [{
        'kind': 'emoji_selected',
        'hash': _send_ref_content_hash(send_ref)[:8],
        'emotion': '开心',
        'tags': '开心,可爱',
        'candidateCount': 1,
        'useCountBefore': 0,
    }]
    selected_events = [item for item in spy_trace if item['kind'] == 'emoji_selected']
    assert len(selected_events) == 1
    assert selected_events[0]['emotion'] == '开心'
    assert selected_events[0]['hash'] == _send_ref_content_hash(send_ref)
    assert selected_events[0]['tags'] == '开心,可爱'
    assert selected_events[0]['candidateCount'] == 1
    assert selected_events[0]['useCountBefore'] == 0


async def test_consume_events_stores_missed_side_effect(
    db: sqlite3.Connection, tmp_path: Path,
) -> None:
    """落空时不出站，但副作用里留下目标情绪，面板据此渲染落空那一行。"""
    library = EmojiLibrary(db, tmp_path / 'emojis')
    await library.register(_png_bytes('blue'), '睡觉,打哈欠', 'image/png')
    chat = _chat(db, library)
    context = _qq_group_context(db)
    sink = _sink(context)

    await chat._consume_events([EmojiEvent(emotion='量子涨落')], sink)

    assert sink.emoji_items == []
    assert sink.side_effects == [{
        'kind': 'emoji_selection_missed',
        'emotion': '量子涨落',
    }]


async def test_capacity_check_excludes_banned_rows(
    db: sqlite3.Connection, tmp_path: Path,
) -> None:
    """封禁行不占容量：库里 3 行 1 行已封时，上限 2 不算超限、上限 1 才算。"""
    library = EmojiLibrary(db, tmp_path / 'emojis')
    red = hashlib.sha256(_png_bytes('red')).hexdigest()
    await library.register(_png_bytes('red'), '开心', 'image/png')
    await library.register(_png_bytes('white'), '高兴', 'image/png')
    await library.register(_png_bytes('blue'), '无语', 'image/png')
    assert library.ban(red) is True

    stats = library.stats()
    assert stats['count'] == 3
    assert stats['countedCount'] == 2
    assert _emoji_capacity_exceeded(stats, 2) is False
    assert _emoji_capacity_exceeded(stats, 1) is True
    # 0 表示不设限，任何库容量都不构成超限。
    assert _emoji_capacity_exceeded(stats, 0) is False
