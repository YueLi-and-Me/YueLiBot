from io import BytesIO
from pathlib import Path

import base64
import hashlib
import sqlite3
import struct

import pytest
from PIL import Image

from src.core.agent.action_protocol import DecisionHead
from src.core.agent.parser import EmojiEvent, ResponseParser
from src.core.db.migrations.manager import CURRENT_VERSION, run_migrations
from src.core.db.schema import DDL
from src.core.config.schema import Config, VisionConfig
from src.core.platform_io.drivers.qq_ws import QqWebSocketDriver
from src.core.platform_io.types import OutboundMessage, StreamRef
from src.core.services.media.chat_image import (
    ChatImageDescriber,
    DescribedEmoji,
    merge_emoji_descriptions,
)
from src.core.services.media.emoji import EmojiIntegrityError, EmojiLibrary
from src.platforms.onebot11.backend import _parse_outbound
from src.platforms.onebot11.config import GroupAccessConfig, PrivateAccessConfig
from src.platforms.onebot11.events import parse_inbound_event
from src.platforms.onebot11.segments import (
    emoji_source_urls,
    emoji_sub_types,
    image_source_urls,
    outbound_message_segments,
)


class _EmbeddingClient:
    dim = 2

    async def embed_one(self, text: str) -> bytes | None:
        if text in {'开心', '高兴,愉快'}:
            return struct.pack('2f', 1.0, 0.0)
        if text == '无语':
            return struct.pack('2f', 0.0, 1.0)
        return None


class _UnconfiguredDimEmbeddingClient:
    """模拟 embedding_dim 未填写（0）的客户端，维度信息只由向量字节承载。"""

    dim = 0

    async def embed_one(self, text: str) -> bytes | None:
        if '开心' in text or '高兴' in text or '乐' in text:
            return struct.pack('3f', 1.0, 0.0, 0.0)
        if '无语' in text or '无奈' in text:
            return struct.pack('3f', 0.0, 1.0, 0.0)
        return None


class _VisionProvider:
    model = 'vision-test'

    async def stream(self, messages: list[dict], **_kwargs):
        prompt = messages[0]['content'][0]['text']
        yield {
            'text': (
                '好笑，无语，调侃'
                if '情绪或语气标签' in prompt
                else '一只猫正在挥手'
            )
        }


class _UnexpectedVisionProvider:
    """已有标签命中时不得调用的视觉模型替身。"""

    model = 'vision-unexpected'

    async def stream(self, _messages: list[dict], **_kwargs):
        raise AssertionError('已有 emotion_tags 时不应调用视觉模型')
        if False:
            yield {'text': ''}


class _EmojiDescriber:
    """按测试图片内容返回固定情绪标签，并记录实际模型候选。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sources: list[str] = []

    async def describe_emoji_sources(
        self,
        sources: list[str],
    ) -> list[DescribedEmoji | None]:
        self.sources.extend(sources)
        if self.fail:
            return [None for _source in sources]
        results: list[DescribedEmoji | None] = []
        for source in sources:
            image_bytes = Path(source).read_bytes()
            results.append(DescribedEmoji(
                content_hash=hashlib.sha256(image_bytes).hexdigest(),
                emotion_tags='开心,可爱,兴奋',
                image_bytes=image_bytes,
                media_type='image/png',
            ))
        return results


def _database() -> sqlite3.Connection:
    db = sqlite3.connect(':memory:')
    db.executescript(DDL)
    return db


def _png_bytes(color: str = 'red') -> bytes:
    """生成可被 Pillow 完整校验的小型 PNG 测试素材。"""

    stream = BytesIO()
    Image.new('RGB', (2, 2), color=color).save(stream, format='PNG')
    return stream.getvalue()


def test_parser_accepts_emoji_visible_product_without_say() -> None:
    parser = ResponseParser()
    events = parser.push('<emoji emotion="开心"/>') + parser.flush()

    assert events == [EmojiEvent(emotion='开心')]
    decision = DecisionHead(
        action='reply',
        target_message_ids=(1,),
        quote_message_id=None,
        reason_codes=('natural_reaction',),
        length='brief',
    ).to_decision('', ('开心',))
    assert decision.reply is not None
    assert decision.reply.text == ''
    assert decision.reply.emoji_emotions == ('开心',)


def test_merge_emoji_descriptions_preserves_failed_placeholder() -> None:
    described = DescribedEmoji(
        content_hash='a' * 64,
        emotion_tags='好笑,无语,调侃,崩溃',
        image_bytes=b'image',
        media_type='image/png',
    )

    assert merge_emoji_descriptions(
        '[表情包]中间[表情包]',
        [described, None],
    ) == '[表情包：好笑,无语]中间[表情包]'
    assert described.emotion_tags == '好笑,无语,调侃,崩溃'


@pytest.mark.asyncio
async def test_emoji_uses_distinct_vlm_prompt_and_content_hash() -> None:
    cfg = Config(vision=VisionConfig(chat_image_enabled=True))
    describer = ChatImageDescriber(cfg, _VisionProvider())
    image_bytes = b'fake-jpeg-content'
    source = 'base64://' + base64.b64encode(image_bytes).decode('ascii')

    emojis = await describer.describe_emoji_sources([source])
    ordinary = await describer.describe(image_bytes)

    assert emojis[0] is not None
    assert emojis[0].emotion_tags == '好笑,无语,调侃'
    assert emojis[0].image_bytes == image_bytes
    assert ordinary == '一只猫正在挥手'


@pytest.mark.asyncio
async def test_emoji_description_reuses_library_tags_before_vision(
    tmp_path: Path,
) -> None:
    """哈希已登记且标签非空时直接查表，视觉模型调用必须为零。"""
    db = _database()
    image_bytes = _png_bytes()
    library = EmojiLibrary(db, tmp_path / 'emojis')
    await library.register(image_bytes, '疲惫,无奈,委屈', 'image/png')
    describer = ChatImageDescriber(
        Config(vision=VisionConfig(chat_image_enabled=True)),
        _UnexpectedVisionProvider(),
        emoji_tag_lookup=library.emotion_tags_for_hash,
    )
    source = 'base64://' + base64.b64encode(image_bytes).decode('ascii')

    descriptions = await describer.describe_emoji_sources([source])

    assert descriptions[0] is not None
    assert descriptions[0].emotion_tags == '疲惫,无奈,委屈'


@pytest.mark.asyncio
async def test_library_registers_selects_and_verifies_hash(tmp_path: Path) -> None:
    db = _database()
    library = EmojiLibrary(
        db,
        tmp_path / 'emojis',
        _EmbeddingClient(),
        choice=lambda candidates: candidates[0],
    )
    happy_ref = await library.register(b'happy-image', '高兴,愉快', 'image/png')
    await library.register(b'speechless-image', '无语', 'image/gif')

    assert library.verify_integrity() == 2
    selected = await library.select('开心')
    assert selected is not None
    assert (selected.send_ref, selected.sub_type) == (happy_ref, 1)
    assert db.execute(
        'SELECT seen_count FROM emoji WHERE send_ref = ?',
        (happy_ref,),
    ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_select_matches_semantically_when_config_dim_unfilled(
    tmp_path: Path,
) -> None:
    """embedding_dim 未配置（0）时语义排序仍可用，不退化为字面匹配。

    回归背景：按配置维度推导期望字节长度会把库内全部向量判为维度不符，
    「乐呵」这类不在标签里的近义词将永远落空。
    """
    db = _database()
    library = EmojiLibrary(
        db,
        tmp_path / 'emojis',
        _UnconfiguredDimEmbeddingClient(),
        choice=lambda candidates: candidates[0],
    )
    happy_ref = await library.register(b'happy-image', '高兴,愉快', 'image/png')
    await library.register(b'speechless-image', '无语', 'image/gif')

    selected = await library.select('乐呵')

    assert selected is not None
    assert selected.send_ref == happy_ref


@pytest.mark.asyncio
async def test_select_skips_vectors_of_stale_dimension(tmp_path: Path) -> None:
    """库内残留其它维度的历史向量时逐条跳过，不拖垮整批候选。"""
    db = _database()
    library = EmojiLibrary(
        db,
        tmp_path / 'emojis',
        _EmbeddingClient(),
        choice=lambda candidates: candidates[0],
    )
    happy_ref = await library.register(b'happy-image', '高兴,愉快', 'image/png')
    stale_ref = await library.register(b'speechless-image', '无语', 'image/gif')
    db.execute(
        'UPDATE emoji SET emotion_vec = ? WHERE send_ref = ?',
        (struct.pack('3f', 0.0, 1.0, 0.0), stale_ref),
    )

    selected = await library.select('开心')

    assert selected is not None
    assert selected.send_ref == happy_ref


@pytest.mark.asyncio
async def test_startup_auto_registers_new_image_with_vision_tags(
    tmp_path: Path,
) -> None:
    db = _database()
    directory = tmp_path / 'emojis'
    directory.mkdir()
    source = directory / 'cat.png'
    source.write_bytes(_png_bytes())
    describer = _EmojiDescriber()
    library = EmojiLibrary(db, directory, _EmbeddingClient())

    first = await library.auto_register_directory(describer)
    second = await library.auto_register_directory(describer)

    assert (first.discovered, first.added, first.skipped, first.failed) == (1, 1, 0, 0)
    assert (second.discovered, second.added, second.skipped, second.failed) == (2, 0, 2, 0)
    assert db.execute('SELECT emotion_tags, seen_count FROM emoji').fetchone() == (
        '开心,可爱,兴奋',
        1,
    )
    assert source.exists()
    assert describer.sources == [str(source.resolve())]
    assert library.verify_integrity() == 1


@pytest.mark.asyncio
async def test_startup_auto_register_deduplicates_before_vision_call(
    tmp_path: Path,
) -> None:
    db = _database()
    directory = tmp_path / 'emojis'
    directory.mkdir()
    image_bytes = _png_bytes()
    (directory / 'first.png').write_bytes(image_bytes)
    (directory / 'second.png').write_bytes(image_bytes)
    describer = _EmojiDescriber()
    library = EmojiLibrary(db, directory)

    summary = await library.auto_register_directory(describer)

    assert (summary.discovered, summary.added, summary.skipped, summary.failed) == (2, 1, 1, 0)
    assert len(describer.sources) == 1
    assert db.execute('SELECT COUNT(*) FROM emoji').fetchone() == (1,)


@pytest.mark.asyncio
async def test_startup_auto_register_keeps_vlm_failure_unregistered(
    tmp_path: Path,
) -> None:
    db = _database()
    directory = tmp_path / 'emojis'
    directory.mkdir()
    source = directory / 'cat.png'
    source.write_bytes(_png_bytes())
    library = EmojiLibrary(db, directory)

    summary = await library.auto_register_directory(_EmojiDescriber(fail=True))

    assert (summary.discovered, summary.added, summary.skipped, summary.failed) == (1, 0, 0, 1)
    assert db.execute('SELECT COUNT(*) FROM emoji').fetchone() == (0,)
    assert source.exists()


@pytest.mark.asyncio
async def test_startup_auto_register_rejects_corrupt_image_before_vision(
    tmp_path: Path,
) -> None:
    db = _database()
    directory = tmp_path / 'emojis'
    directory.mkdir()
    (directory / 'broken.png').write_bytes(b'not-an-image')
    describer = _EmojiDescriber()
    library = EmojiLibrary(db, directory)

    summary = await library.auto_register_directory(describer)

    assert (summary.discovered, summary.added, summary.skipped, summary.failed) == (1, 0, 0, 1)
    assert describer.sources == []
    assert db.execute('SELECT COUNT(*) FROM emoji').fetchone() == (0,)


@pytest.mark.asyncio
async def test_integrity_check_rejects_corrupted_file(tmp_path: Path) -> None:
    library = EmojiLibrary(_database(), tmp_path / 'emojis')
    send_ref = await library.register(b'original', '开心', 'image/png')
    path = Path(send_ref.removeprefix('file:///'))
    if not path.is_absolute():
        path = Path('/' + str(path))
    path.write_bytes(b'corrupted')

    with pytest.raises(EmojiIntegrityError, match='内容哈希不一致'):
        library.verify_integrity()


@pytest.mark.asyncio
async def test_text_fallback_does_not_send_unrelated_emoji(tmp_path: Path) -> None:
    library = EmojiLibrary(
        _database(),
        tmp_path / 'emojis',
        choice=lambda candidates: candidates[0],
    )
    expected = await library.register(b'happy', '开心,高兴', 'image/png')

    selected = await library.select('很开心')
    assert selected is not None
    assert (selected.send_ref, selected.sub_type) == (expected, 1)
    assert await library.select('生气') is None


@pytest.mark.asyncio
async def test_frequent_tags_orders_by_coverage(tmp_path: Path) -> None:
    """高频标签按覆盖表情数降序，同数按字典序；limit 与非法入参各自生效。"""
    db = _database()
    library = EmojiLibrary(db, tmp_path / 'emojis', _EmbeddingClient())
    await library.register(b'a-image', '开心,可爱', 'image/png')
    await library.register(b'b-image', '开心,无语', 'image/png')
    await library.register(b'c-image', '无语', 'image/png')

    assert library.frequent_tags() == ('开心', '无语', '可爱')
    assert library.frequent_tags(2) == ('开心', '无语')
    with pytest.raises(ValueError):
        library.frequent_tags(0)


def test_napcat_keeps_normal_and_emoji_sources_separate() -> None:
    segments = [
        {'type': 'image', 'data': {'sub_type': 0, 'url': 'https://example/a.png'}},
        {'type': 'image', 'data': {
            'sub_type': 1,
            'url': 'https://example/e.gif',
            'file': 'e.gif',
        }},
    ]

    assert image_source_urls(segments) == ('https://example/a.png',)
    assert emoji_source_urls(segments) == ('https://example/e.gif',)
    assert emoji_sub_types(segments) == (1,)


def test_napcat_inbound_event_keeps_emoji_sub_type_aligned() -> None:
    event = parse_inbound_event(
        {
            'post_type': 'message',
            'message_id': 42,
            'group_id': 123,
            'sender': {'user_id': 456, 'nickname': '测试用户'},
            'message': [{
                'type': 'image',
                'data': {'sub_type': '7', 'url': 'https://example/e.png'},
            }],
        },
        self_id='999',
        self_name='月璃',
        owner_qq='111',
        private_access=PrivateAccessConfig(),
        group_access=GroupAccessConfig(list=['123']),
    )

    assert event is not None
    assert event.emoji_sources == ('https://example/e.png',)
    assert event.emoji_sub_types == (7,)


@pytest.mark.asyncio
async def test_emoji_sub_type_round_trips_through_library_and_outbound(
    tmp_path: Path,
) -> None:
    segments = [{
        'type': 'image',
        'data': {'sub_type': 7, 'url': 'https://example/e.png'},
    }]
    library = EmojiLibrary(
        _database(),
        tmp_path / 'emojis',
        choice=lambda candidates: candidates[0],
    )
    await library.register(
        b'emoji-image',
        '开心',
        'image/png',
        sub_type=emoji_sub_types(segments)[0],
    )

    selected = await library.select('开心')

    assert selected is not None
    assert outbound_message_segments(
        [],
        (selected.send_ref,),
        (selected.sub_type,),
    ) == [{
        'type': 'image',
        'data': {'file': selected.send_ref, 'sub_type': 7},
    }]


def test_backend_outbound_allows_emoji_only_message() -> None:
    outbound = _parse_outbound({
        'stream_id': 7,
        'channel': 'qq.send',
        'payload': {
            'streamKind': 'group',
            'streamExternalId': '123',
            'segments': [],
            'emojiRefs': ['file:///D:/data/emojis/a.png'],
            'emojiSubTypes': [1],
        },
    })

    assert outbound.segments == []
    assert outbound.emoji_refs == ('file:///D:/data/emojis/a.png',)
    assert outbound.emoji_sub_types == (1,)
    assert outbound_message_segments(
        outbound.segments,
        outbound.emoji_refs,
        outbound.emoji_sub_types,
    ) == [{
        'type': 'image',
        'data': {'file': 'file:///D:/data/emojis/a.png', 'sub_type': 1},
    }]


@pytest.mark.asyncio
async def test_qq_driver_transmits_emoji_references() -> None:
    pushed: list[tuple[int, str, dict]] = []

    async def push(stream_id: int, channel: str, payload: dict) -> int:
        pushed.append((stream_id, channel, payload))
        return 1

    driver = QqWebSocketDriver(push)
    await driver.send(OutboundMessage(
        stream=StreamRef(id=7, platform='qq', kind='group', external_id='123'),
        segments=[],
        emoji_refs=('file:///D:/data/emojis/a.png',),
        emoji_sub_types=(1,),
    ))

    assert pushed == [(7, 'qq.send', {
        'streamKind': 'group',
        'streamExternalId': '123',
        'segments': [],
        'emojiRefs': ['file:///D:/data/emojis/a.png'],
        'emojiSubTypes': [1],
    })]


def test_v9_database_migrates_to_emoji_schema() -> None:
    db = _database()
    db.execute('DROP TABLE emoji')
    db.execute('PRAGMA user_version = 9')

    run_migrations(db)

    assert db.execute('PRAGMA user_version').fetchone() == (CURRENT_VERSION,)
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'emoji'"
    ).fetchone() == (1,)
    assert db.execute(
        "SELECT name FROM pragma_table_info('emoji') WHERE name = 'sub_type'"
    ).fetchone() == ('sub_type',)


def test_v10_database_defaults_existing_emoji_to_sticker_sub_type() -> None:
    db = _database()
    db.execute('DROP TABLE emoji')
    db.execute(
        '''CREATE TABLE emoji (
               hash          TEXT PRIMARY KEY,
               send_ref      TEXT NOT NULL,
               emotion_tags  TEXT NOT NULL,
               emotion_vec   BLOB,
               seen_count    INTEGER NOT NULL DEFAULT 1,
               first_seen_at INTEGER NOT NULL
           )'''
    )
    db.execute(
        '''INSERT INTO emoji (
               hash, send_ref, emotion_tags, emotion_vec, seen_count, first_seen_at
           ) VALUES (?, ?, ?, NULL, 1, 1)''',
        ('a' * 64, 'file:///D:/data/emojis/a.png', '开心'),
    )
    db.execute('PRAGMA user_version = 10')

    run_migrations(db)

    assert db.execute('PRAGMA user_version').fetchone() == (CURRENT_VERSION,)
    assert db.execute('SELECT sub_type FROM emoji').fetchone() == (1,)


@pytest.mark.asyncio
async def test_banned_entries_excluded_from_capacity_and_eviction(tmp_path: Path) -> None:
    """已封禁的记录既不计入容量、也不会被淘汰。

    两件事必须同时成立。只把封禁行排除出计数、却仍让它们进入淘汰候选，会出现
    「删了但计数不降」的空转：被封的行 use_count 通常为 0，排在淘汰序最前，会
    被一条条删光才轮到真正该淘汰的。
    """

    db = _database()
    library = EmojiLibrary(db, tmp_path / 'emojis', _EmbeddingClient())
    for index in range(5):
        await library.register(f'image-{index}'.encode(), f'情绪{index}', 'image/png')

    banned_hash = library.page(limit=1)[0]['hash']
    assert library.ban(banned_hash, '测试') is True

    stats = library.stats()
    assert stats['count'] == 5
    assert stats['bannedInLibrary'] == 1
    assert stats['countedCount'] == 4

    # 上限 4：未封禁的正好 4 条，已封禁那条不占名额，因此不该淘汰任何东西。
    assert library.evict_to_limit(4) == []
    assert library.stats()['count'] == 5

    # 上限 3：只能动未封禁的那 4 条，封禁行必须原样留着。
    evicted = library.evict_to_limit(3)
    assert len(evicted) == 1
    assert evicted[0].content_hash != banned_hash
    assert library.stats()['countedCount'] == 3
    assert {row['hash'] for row in library.page(limit=50, banned_only=True)} == {banned_hash}


@pytest.mark.asyncio
async def test_page_and_count_filter_by_banned(tmp_path: Path) -> None:
    """列表与计数按封禁状态筛选，三种取值互补且覆盖全集。"""

    db = _database()
    library = EmojiLibrary(db, tmp_path / 'emojis', _EmbeddingClient())
    for index in range(4):
        await library.register(f'image-{index}'.encode(), f'情绪{index}', 'image/png')
    banned_hash = library.page(limit=1)[0]['hash']
    library.ban(banned_hash, '')

    assert library.count_entries(banned_only=None) == 4
    assert library.count_entries(banned_only=True) == 1
    assert library.count_entries(banned_only=False) == 3

    only_banned = library.page(limit=50, banned_only=True)
    assert [row['hash'] for row in only_banned] == [banned_hash]
    assert only_banned[0]['banned'] is True

    active = library.page(limit=50, banned_only=False)
    assert banned_hash not in {row['hash'] for row in active}
    assert all(row['banned'] is False for row in active)


@pytest.mark.asyncio
async def test_ban_on_missing_row_still_counted_separately(tmp_path: Path) -> None:
    """封禁独立于 emoji 行存在，bannedCount 可以大于 bannedInLibrary。

    图被淘汰或手动删除后封禁依然生效（这正是封禁按内容哈希独立存放的目的），
    此时它不再对应任何一行，不该被算进「库里有多少条被封」。
    """

    db = _database()
    library = EmojiLibrary(db, tmp_path / 'emojis', _EmbeddingClient())
    await library.register(b'image-0', '情绪', 'image/png')
    target = library.page(limit=1)[0]['hash']
    library.ban(target, '')
    assert library.remove(target) is True

    stats = library.stats()
    assert stats['count'] == 0
    assert stats['bannedCount'] == 1
    assert stats['bannedInLibrary'] == 0
    assert stats['countedCount'] == 0
