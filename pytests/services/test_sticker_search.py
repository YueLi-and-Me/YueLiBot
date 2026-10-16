"""按画面内容查表情包的回归。

钉住：候选排序（描述覆盖分优先、其次画面向量）、检索词按片段计分、封禁不进候选、
按编号取图、宫格图尺寸与编号、挑选与复核输出的严格解析、弃权与复核否决、视觉模型
不可用时工具失败、工具只在本轮能发表情包时声明、画面描述的后台补写、标签里的模型
控制符号被清洗，以及回复里按编号发送查到的那一张。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List

import asyncio
import hashlib
import sqlite3
import struct

from PIL import Image

import pytest

from src.core.agent.action_protocol import DecisionFrame, PlatformCapabilities, available_actions
from src.core.agent.parser import EmojiEvent, ResponseParser
from src.core.config.schema import Config
from src.core.platform_io.registry import StreamRegistry
from src.core.platform_io.types import DeliveryReceipt
from src.core.services.chat import ChatService
from src.core.services.chat.state import _TurnSink
from src.core.services.media.chat_image import _normalize_emoji_content, _normalize_emotion_tags
from src.core.services.media.emoji import EmojiLibrary, sticker_id
from src.core.services.media.emoji_content import EmojiContentIndexer
from src.core.services.media.sticker_search import (
    STICKER_SEARCH_SPEC,
    StickerSearch,
    compose_sticker_grid,
    parse_pick,
    parse_verdict,
    render_sticker_frame,
)
from src.core.tooling.registry import build_builtin_action_registry
from src.core.tooling.spec import ToolContext, ToolInvocation


def _png(color: str, size: int = 2) -> bytes:
    """生成灰度差异明显的纯色 PNG；视觉身份按灰度缩略图判重，颜色要拉开。"""
    stream = BytesIO()
    Image.new('RGB', (size, size), color=color).save(stream, format='PNG')
    return stream.getvalue()


def _digest(image: bytes) -> str:
    return hashlib.sha256(image).hexdigest()


def _vec(*values: float) -> bytes:
    return struct.pack(f'<{len(values)}f', *values)


class _FakeEmbed:
    """按文本查表返回固定向量的替身；未登记的文本返回 None。"""

    accepts_images = False
    dim = 3

    def __init__(self, table: Dict[str, bytes]) -> None:
        self._table = table

    async def embed_one(self, text: str) -> bytes | None:
        return self._table.get(text)

    async def embed_inputs(self, items: List[Any]) -> List[bytes | None]:
        return [self._table.get(item.text) for item in items]


class _FakeVision:
    """记录每次请求并依次按预设输出回答的视觉模型替身。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.requests: list[list[Any]] = []

    async def stream(self, messages: list[Any], **_kwargs: Any):
        self.requests.append(messages)
        yield {'text': self.replies.pop(0)}


def _frame(*, emoji: bool) -> DecisionFrame:
    caps = PlatformCapabilities(emoji=emoji)
    return DecisionFrame(
        turn_id=7,
        snapshot_id='snap-7',
        stream_kind='group',
        disposition='deliberate',
        selectable_message_ids=(101,),
        message_watermark=101,
        available_actions=available_actions('group', 'deliberate', caps, cognitive_rounds_left=2),
        capabilities=caps,
    )


def _context() -> ToolContext:
    return ToolContext(
        stream_id=3, stream_kind='group', frame=_frame(emoji=True), turn_id=7, snapshot_id='snap-7',
    )


async def _library_with_three(db: sqlite3.Connection, tmp_path: Path) -> tuple[EmojiLibrary, list[str]]:
    """三张图：红色向量最接近检索词，白色带命中描述，蓝色两者都不沾。"""
    library = EmojiLibrary(db, tmp_path / 'emojis', _FakeEmbed({
        '开心': _vec(1, 0, 0),
        '无语': _vec(0, 1, 0),
        '平静': _vec(0, 0, 1),
        'DeepSeek娘 女仆': _vec(0.9, 0, 0.1),
    }))
    images = [_png('red'), _png('white'), _png('blue')]
    for image, tags in zip(images, ('开心', '无语', '平静')):
        await library.register(image, tags, 'image/png')
    hashes = [_digest(image) for image in images]
    assert library.set_description(hashes[1], 'DeepSeek娘（蓝发鲸鱼尾女仆），文字：鲸元卷')
    return library, hashes


async def test_content_candidates_rank_description_hits_before_vectors(
    db: sqlite3.Connection, tmp_path: Path,
) -> None:
    """描述命中检索词的排最前；其余按画面向量相似度排。"""
    library, hashes = await _library_with_three(db, tmp_path)

    candidates = await library.content_candidates('DeepSeek娘 女仆', 3)

    assert [candidate.content_hash for candidate in candidates] == [hashes[1], hashes[0], hashes[2]]
    assert candidates[0].description.startswith('DeepSeek娘')


async def test_content_candidates_score_term_pieces(
    db: sqlite3.Connection, tmp_path: Path,
) -> None:
    """检索词整词对不上时按 jieba 词与二字组的覆盖比例计分，不必与描述逐字相同。"""
    library, hashes = await _library_with_three(db, tmp_path)
    assert library.set_description(hashes[2], '粉发粉眼动漫少女，在键盘上敲字')

    candidates = await library.content_candidates('粉发少女 敲键盘', 3)

    assert candidates[0].content_hash == hashes[2]


async def test_content_candidates_exclude_banned(db: sqlite3.Connection, tmp_path: Path) -> None:
    library, hashes = await _library_with_three(db, tmp_path)
    assert library.ban(hashes[1]) is True

    candidates = await library.content_candidates('DeepSeek娘 女仆', 3)

    assert hashes[1] not in [candidate.content_hash for candidate in candidates]
    with pytest.raises(ValueError):
        await library.content_candidates('  ', 3)


async def test_select_by_id_resolves_prefix_and_skips_banned(
    db: sqlite3.Connection, tmp_path: Path,
) -> None:
    library, hashes = await _library_with_three(db, tmp_path)

    selection = library.select_by_id(sticker_id(hashes[0]))

    assert selection is not None
    assert selection.send_ref.endswith(f'{hashes[0]}.png')
    assert library.select_by_id('不是编号') is None
    assert library.select_by_id(sticker_id(hashes[0])[:6]) is None
    library.ban(hashes[0])
    assert library.select_by_id(sticker_id(hashes[0])) is None


async def test_pending_content_lists_undescribed_unbanned(
    db: sqlite3.Connection, tmp_path: Path,
) -> None:
    library, hashes = await _library_with_three(db, tmp_path)
    library.ban(hashes[2])

    pending = library.pending_content(10)

    assert [digest for digest, _ref in pending] == [hashes[0]]


def test_compose_grid_size_and_labels() -> None:
    """5 张排成 3 列 2 行；每格左上角是黑底编号方块。"""
    grid = compose_sticker_grid([_png(color, 40) for color in ('red', 'white', 'blue', 'yellow', 'gray')])

    with Image.open(BytesIO(grid)) as image:
        assert image.size == (768, 512)
        assert max(image.getpixel((3, 3))) < 40
        assert max(image.getpixel((256 + 3, 256 + 3))) < 40


@pytest.mark.parametrize(('raw', 'expected'), [('3', 3), ('0', 0), (' 9\n', 9), ('<think>想想</think>2', 2)])
def test_parse_pick_accepts_number_or_abstain(raw: str, expected: int) -> None:
    assert parse_pick(raw, 9) == expected


@pytest.mark.parametrize('raw', ['10', '第 2 张', '2, 3', ''])
def test_parse_pick_rejects_out_of_range_or_prose(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_pick(raw, 9)


@pytest.mark.parametrize(('raw', 'expected'), [('是', True), ('否。', False), ('<think>嗯</think>是\n', True)])
def test_parse_verdict_accepts_yes_or_no(raw: str, expected: bool) -> None:
    assert parse_verdict(raw) is expected


@pytest.mark.parametrize('raw', ['是的', '不是', ''])
def test_parse_verdict_rejects_prose(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_verdict(raw)


def test_render_sticker_frame_caps_edge() -> None:
    with Image.open(BytesIO(render_sticker_frame(_png('red', 1000)))) as image:
        assert image.format == 'JPEG'
        assert image.size == (768, 768)


async def test_search_returns_picked_sticker_id(db: sqlite3.Connection, tmp_path: Path) -> None:
    """视觉模型挑中第 1 格且复核通过：观察正文给出该图编号与画面描述。"""
    library, hashes = await _library_with_three(db, tmp_path)
    vision = _FakeVision('1', '是')
    search = StickerSearch(library, vision, temperature=0.2, max_tokens=None)

    result = await search.execute(
        ToolInvocation(tool_name='find_sticker', arguments={'query': 'DeepSeek娘 女仆'}),
        _context(),
    )

    assert result.success is True
    assert sticker_id(hashes[1]) in result.observation
    assert '鲸元卷' in result.observation
    pick, verify = (request[0]['content'] for request in vision.requests)
    assert 'DeepSeek娘 女仆' in pick[0]['text']
    assert pick[1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert 'DeepSeek娘 女仆' in verify[0]['text']
    assert verify[1]['image_url']['url'] != pick[1]['image_url']['url']


async def test_search_drops_pick_rejected_by_verify(db: sqlite3.Connection, tmp_path: Path) -> None:
    """宫格里挑中了但单图复核说对不上：按没找到处理，不给编号。"""
    library, hashes = await _library_with_three(db, tmp_path)
    search = StickerSearch(library, _FakeVision('1', '否'), temperature=0.2, max_tokens=None)

    result = await search.execute(
        ToolInvocation(tool_name='find_sticker', arguments={'query': '猫猫 敲键盘'}), _context(),
    )

    assert result.success is True
    assert '没有符合「猫猫 敲键盘」' in result.observation
    assert all(sticker_id(digest) not in result.observation for digest in hashes)


async def test_search_abstains_without_sending(db: sqlite3.Connection, tmp_path: Path) -> None:
    library, _hashes = await _library_with_three(db, tmp_path)
    search = StickerSearch(library, _FakeVision('0'), temperature=0.2, max_tokens=None)

    result = await search.execute(
        ToolInvocation(tool_name='find_sticker', arguments={'query': '大肥鱼'}), _context(),
    )

    assert result.success is True
    assert '没有符合「大肥鱼」' in result.observation


async def test_search_fails_on_unparsable_pick_or_missing_vision(
    db: sqlite3.Connection, tmp_path: Path,
) -> None:
    library, _hashes = await _library_with_three(db, tmp_path)
    invocation = ToolInvocation(tool_name='find_sticker', arguments={'query': 'DeepSeek娘'})

    unparsable = await StickerSearch(
        library, _FakeVision('我觉得是第二张'), temperature=0.2, max_tokens=None,
    ).execute(invocation, _context())
    unparsable_verify = await StickerSearch(
        library, _FakeVision('1', '应该是吧'), temperature=0.2, max_tokens=None,
    ).execute(invocation, _context())
    no_vision = await StickerSearch(
        library, None, temperature=0.2, max_tokens=None,
    ).execute(invocation, _context())

    assert unparsable.success is False
    assert unparsable_verify.success is False
    assert no_vision.success is False
    assert '视觉模型不可用' in no_vision.error_message


def test_tool_declared_only_when_emoji_allowed() -> None:
    registry = build_builtin_action_registry()
    registry.register_tool(STICKER_SEARCH_SPEC, StickerSearch(None, None, temperature=0, max_tokens=None))

    assert 'find_sticker' in registry.available_tool_names(_frame(emoji=True))
    assert 'find_sticker' not in registry.available_tool_names(_frame(emoji=False))


class _FakeDescriber:
    """按哈希返回预设描述的识别服务替身。"""

    def __init__(self, replies: Dict[str, str | None], *, available: bool = True) -> None:
        self.replies = replies
        self.available = available
        self.calls: List[str] = []

    def can_describe_emoji_content(self) -> bool:
        return self.available

    async def describe_emoji_content(self, image_bytes: bytes, media_type: str, digest: str) -> str | None:
        self.calls.append(digest)
        return self.replies.get(digest)


async def test_indexer_fills_descriptions_once_per_failure(
    db: sqlite3.Connection, tmp_path: Path,
) -> None:
    """成功的写入描述；识别失败的在本进程内不再重试；已封禁的不识别。"""
    library = EmojiLibrary(db, tmp_path / 'emojis')
    images = [_png('red'), _png('white'), _png('blue')]
    for image in images:
        await library.register(image, '开心', 'image/png')
    hashes = [_digest(image) for image in images]
    library.ban(hashes[2])
    describer = _FakeDescriber({hashes[0]: '蓝发鲸鱼尾女仆'})
    indexer = EmojiContentIndexer(library, describer)

    assert await indexer.index_pending() == 1
    assert await indexer.index_pending() == 0

    assert describer.calls == [hashes[0], hashes[1]]
    assert library.pending_content(10) == [(hashes[1], library.pending_content(10)[0][1])]
    assert await EmojiContentIndexer(library, _FakeDescriber({}, available=False)).index_pending() == 0


def test_model_markup_is_stripped_from_tags_and_content() -> None:
    assert _normalize_emotion_tags('调侃,戏谑,幽默<|end_of_box|>') == '调侃,戏谑,幽默'
    assert _normalize_emotion_tags('可爱,温柔,呆萌,萌</think>') == '可爱,温柔,呆萌,萌'
    assert _normalize_emoji_content('<think>先看看</think>蓝发女仆\n手拿钞票<|end_of_box|>') == '蓝发女仆 手拿钞票'


def test_parser_reads_sticker_id() -> None:
    events = ResponseParser().push('<say emotion="smile">给你</say><emoji id="37be5a50"/>')

    emoji = [event for event in events if isinstance(event, EmojiEvent)]
    assert emoji == [EmojiEvent(emotion='', sticker_id='37be5a50')]
    assert emoji[0].label == '编号 37be5a50'


class _Broker:
    async def dispatch(self, message: Any) -> DeliveryReceipt:
        return DeliveryReceipt(platform=message.stream.platform, stream_id=message.stream.id, external_message_ids=[])


async def _noop(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
    return None


async def test_reply_sends_sticker_by_id(db: sqlite3.Connection, tmp_path: Path) -> None:
    """回复里写了编号：发的就是那一张，不按情绪重新检索；编号失效时记为落空。"""
    library, hashes = await _library_with_three(db, tmp_path)
    chat = ChatService(db, None, None, None, _noop, cfg=Config(), emoji_library=library, broker=_Broker())
    context = StreamRegistry(db).resolve_inbound(
        platform='qq', stream_kind='group', stream_external_id='group-7',
        sender_external_id='contact-42', sender_nickname='账号昵称', sender_group_card='小李',
        first_seen_at=1_700_000_000_000,
    )
    sink = _TurnSink(context=context, cancel_event=asyncio.Event(), turn=9, now=1_700_000_100_000,
                     source_text='来张图')

    await chat._consume_events([EmojiEvent(sticker_id=sticker_id(hashes[1]))], sink)

    assert [ref for _label, ref, _sub_type in sink.emoji_items] == [
        library.select_by_id(sticker_id(hashes[1])).send_ref
    ]
    assert sink.emoji_items[0][0] == '无语'
    assert sink.side_effects[0]['stickerId'] == sticker_id(hashes[1])

    missed = _TurnSink(context=context, cancel_event=asyncio.Event(), turn=10, now=1_700_000_100_000,
                       source_text='来张图')
    await chat._consume_events([EmojiEvent(sticker_id='deadbeef')], missed)
    assert missed.emoji_items == []
    assert missed.side_effects[0]['kind'] == 'emoji_selection_missed'
