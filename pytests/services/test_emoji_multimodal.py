"""表情包专用多模态槽、首帧预处理与可取消补算的假客户端验收。"""

from io import BytesIO
import asyncio
import struct

from PIL import Image
import pytest

from src.core.services.media.emoji import EmojiLibrary


def picture(size=(12, 8), color='red', mode='RGBA'):
    output = BytesIO()
    Image.new(mode, size, color).save(output, format='PNG')
    return output.getvalue()


class FakeEmbedding:
    dim = 2
    accepts_images = True

    def __init__(self):
        self.inputs = []
        self.texts = []
        self.wait = None

    async def embed_inputs(self, items):
        self.inputs.extend(items)
        if self.wait is not None:
            await self.wait.wait()
        return [struct.pack('2f', 1, 0) for _ in items]

    async def embed_one(self, text):
        self.texts.append(text)
        return struct.pack('2f', 1, 0)


def test_preprocess_first_frame_and_size():
    from src.core.services.media.emoji import preprocess_embedding_image
    output = BytesIO()
    Image.new('RGB', (1600, 800), 'red').save(output, format='GIF', save_all=True,
        append_images=[Image.new('RGB', (1600, 800), 'blue')], duration=100, loop=0)
    result = Image.open(BytesIO(preprocess_embedding_image(output.getvalue())))
    assert result.format == 'PNG'
    assert result.size == (1024, 512)
    assert result.convert('RGB').getpixel((0, 0)) == (255, 0, 0)
    transparent = Image.open(BytesIO(preprocess_embedding_image(picture(color=(255, 0, 0, 0)))))
    assert transparent.mode == 'RGBA'
    assert transparent.getpixel((0, 0))[3] == 0


async def test_registration_sends_preprocessed_image_and_tags(db, tmp_path):
    client = FakeEmbedding()
    library = EmojiLibrary(db, tmp_path, client, use_images=True)
    await library.register(picture((1600, 800)), '开心', 'image/png')
    assert len(client.inputs) == 1
    entry = client.inputs[0]
    assert entry.text == '开心' and entry.media_type == 'image/png'
    assert Image.open(BytesIO(entry.image)).size == (1024, 512)
    await library.select('开心')
    assert client.texts == ['开心']


async def test_embedding_fallback_never_sends_image(db, tmp_path):
    # 客户端协议支持图片，并不表示给表情包配置了专用多模态槽。
    client = FakeEmbedding()
    library = EmojiLibrary(db, tmp_path, client, use_images=False)
    await library.register(picture(), '开心', 'image/png')
    assert client.inputs == []
    assert client.texts == ['开心']


async def test_backfill_null_rows_including_banned_and_bad_file(db, tmp_path):
    library = EmojiLibrary(db, tmp_path)
    await library.register(picture(color='red'), '红', 'image/png')
    await library.register(picture(color='blue'), '蓝', 'image/png')
    await library.register(picture(size=(20, 8), color='green'), '绿', 'image/png')
    rows = db.execute('SELECT hash, send_ref FROM emoji ORDER BY emotion_tags').fetchall()
    db.execute('UPDATE emoji SET emotion_vec=? WHERE hash=?', (b'existing', rows[0][0]))
    library.ban(rows[1][0])
    db.execute('UPDATE emoji SET send_ref=? WHERE hash=?', ((tmp_path / 'missing.png').as_uri(), rows[2][0]))
    db.commit()
    client = FakeEmbedding()
    library = EmojiLibrary(db, tmp_path, client, use_images=True)
    assert await library.backfill_embeddings() == 1
    assert len(client.inputs) == 1
    assert db.execute('SELECT emotion_vec FROM emoji WHERE hash=?', (rows[0][0],)).fetchone()[0] == b'existing'
    assert db.execute('SELECT emotion_vec FROM emoji WHERE hash=?', (rows[1][0],)).fetchone()[0] is not None
    assert db.execute('SELECT emotion_vec FROM emoji WHERE hash=?', (rows[2][0],)).fetchone()[0] is None


async def test_startup_returns_immediately_and_shutdown_cancels(db, tmp_path):
    await EmojiLibrary(db, tmp_path).register(picture(), '开心', 'image/png')
    client = FakeEmbedding()
    client.wait = asyncio.Event()
    library = EmojiLibrary(db, tmp_path, client, use_images=True)
    await asyncio.wait_for(library.startup(), timeout=0.2)
    await asyncio.sleep(0)
    task = library._backfill_task
    assert task is not None and not task.done()
    await asyncio.wait_for(library.shutdown(), timeout=0.2)
    assert task.done()
    assert db.execute('SELECT emotion_vec FROM emoji').fetchone()[0] is None


@pytest.mark.parametrize('text_ready,mm_ready,expected', [(False, False, None), (True, False, 'tags'), (True, True, 'image+tags/v1'), (False, True, 'image+tags/v1')])
def test_emoji_space_assembly(text_ready, mm_ready, expected):
    from src.main import _select_emoji_embedding_client
    from src.core.config.schema import Config, ModelCandidate
    from src.core.llm_models.router import create_routers
    from src.core.memory.embed import build_client
    config = Config()
    model = ModelCandidate(name='多模态', provider='p', identifier='mm', api_format='dashscope_multimodal', embedding_dim=2)
    if text_ready:
        config.routing.embedding.candidates = [model]
    if mm_ready:
        config.routing.multimodal_embedding.candidates = [model]
    routers = create_routers(config)
    text = build_client(routers.embedding) if text_ready else None
    client, recipe = _select_emoji_embedding_client(routers, text)
    if expected is None:
        assert client is None
    else:
        assert client is not None and recipe == expected
        assert (client is text) == (not mm_ready)


def test_openai_rejected_in_multimodal_slot():
    from src.core.config.loader import _build_routing
    from src.core.config.schema import CONFIG_VERSION, ApiProviderConfig, ModelCatalog, ModelDefinitionConfig
    model = ModelDefinitionConfig(name='文本模型', model_identifier='text', api_provider='p')
    catalog = ModelCatalog.model_validate({'inner': {'version': CONFIG_VERSION}, 'models': [model],
        'model_tasks': {'multimodal_embedding': {'model_list': [model.name]}}})
    with pytest.raises(ValueError) as exc:
        _build_routing('multimodal_embedding', catalog, {model.name: model}, {'p': ApiProviderConfig(name='p', kind='openai', api_key='fake')}, None)
    assert 'multimodal_embedding' in str(exc.value) and model.name in str(exc.value)


def test_multimodal_slot_roundtrip_and_no_inheritance(tmp_path):
    from pytests.conftest import render_loadable_config
    from src.core.config import model_webui
    from src.core.config.loader import read_config
    config = render_loadable_config(tmp_path)
    initial = read_config(config)
    assert initial.routing.chat.ready
    assert not initial.routing.multimodal_embedding.ready
    snapshot = model_webui.snapshot(config)
    snapshot['models'].append({
        **snapshot['models'][0], 'name': '融合测试', 'model_identifier': 'mm',
        'api_format': 'ark_multimodal', 'embedding_dim': 2,
    })
    snapshot['tasks']['multimodal_embedding']['model_list'] = ['融合测试']
    assert model_webui.save(config, snapshot)['ok']
    loaded = read_config(config)
    assert loaded.routing.multimodal_embedding.candidates[0].api_format == 'ark_multimodal'
    assert model_webui.snapshot(config)['tasks']['multimodal_embedding']['model_list'] == ['融合测试']
