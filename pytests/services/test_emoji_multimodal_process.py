"""启动真实后端子进程；模型请求用假 HTTP 挂起，验证补算不会阻塞 READY。"""

from io import BytesIO
from pathlib import Path
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tomllib

from PIL import Image
import pytest

from src.core.config.bootstrap import render_example_configs
from src.core.config.schema import CONFIG_VERSION
from src.core.config.settings_webui import _write_documented_toml, file_schema
from src.core.db.migrations.manager import run_migrations
from src.core.services.media.emoji import EmojiLibrary


CHILD = '''
from pathlib import Path
import asyncio
import httpx
import sys
from src import main
from src.core.api.state import app_state
from src.core.config import adapter_selection, bootstrap

fixture_adapters = Path(sys.argv[sys.argv.index('--config-path') + 1]) / 'adapters'
adapter_selection.ADAPTERS_ROOT = fixture_adapters
bootstrap.ADAPTERS_ROOT = fixture_adapters

original_client = httpx.AsyncClient
async def fake_model(request):
    assert request.url.host == 'models.invalid', str(request.url)
    if 'embeddings' in request.url.path:
        print('FAKE_EMBEDDING_STARTED=1', flush=True)
        print('FAKE_EMBEDDING_BODY=' + request.content.decode('utf-8'), flush=True)
    await asyncio.sleep(60)
    raise AssertionError('应在假模型返回前关停')
httpx.AsyncClient = lambda **kw: original_client(transport=httpx.MockTransport(fake_model), **kw)

# 保留真实 startup 与就绪公告，仅在监听建立后自动结束受控验收进程。
original_startup = main._ReadyAnnouncingServer.startup
async def startup(self, sockets=None):
    await original_startup(self, sockets)
    task = app_state.emoji_library._backfill_task
    assert task is not None and not task.done()
    print('EMOJI_BACKFILL_PENDING=1', flush=True)
    asyncio.get_running_loop().call_later(0.3, setattr, self, 'should_exit', True)
main._ReadyAnnouncingServer.startup = startup
main.main()
print('CONTROLLED_SHUTDOWN=1', flush=True)
'''


@pytest.mark.parametrize('slot', ['multimodal_embedding', 'embedding'])
def test_real_process_reaches_ready_with_pending_emoji_backfill(tmp_path: Path, slot: str):
    """真实装配下，表情包是否发图只由所接的槽决定。

    ``slot='embedding'`` 时融合假模型挂在 embedding 槽、多模态向量槽留空：协议虽然能收图，
    表情包仍只能发标签文字、登记配方 ``tags``。这一条钉住 main.py 里决定 use_images 的那一行。
    """
    config = tmp_path / 'fixture-config'
    data = tmp_path / 'fixture-data'
    render_example_configs(config)
    repository = Path(__file__).resolve().parents[2]
    for manifest in (repository / 'adapters').glob('*/_manifest.json'):
        target = config / 'adapters' / manifest.parent.name / manifest.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(manifest, target)
    for name in ('providers.toml', 'models.toml', 'features.toml', 'bot.toml'):
        path = config / name
        document = tomllib.loads(path.read_text(encoding='utf-8'))
        if name == 'providers.toml':
            for provider in document['api_providers']:
                provider.update(api_key='fake-process-key', base_url='https://models.invalid/v1')
        elif name == 'models.toml':
            document['models'].append({
                'name': '融合假模型', 'model_identifier': 'mm-fixture', 'api_provider': document['models'][0]['api_provider'],
                'api_format': 'dashscope_multimodal', 'embedding_dim': 2,
            })
            document['model_tasks'][slot]['model_list'] = ['融合假模型']
        elif name == 'features.toml':
            document['telemetry']['enabled'] = False
            document['vision'].update(enabled=False, chat_image_enabled=False, chat_video_enabled=False)
            document['memory_feedback']['enabled'] = False
        else:
            document['desktop_pet']['enabled'] = False
        _write_documented_toml(path, file_schema(name), document, CONFIG_VERSION)
    data.mkdir()
    connection = sqlite3.connect(data / 'memory.db')
    run_migrations(connection)
    image = BytesIO()
    Image.new('RGB', (12, 8), 'red').save(image, format='PNG')
    asyncio.run(EmojiLibrary(connection, data / 'emojis').register(image.getvalue(), '开心', 'image/png'))
    connection.close()
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    result = subprocess.run([
        sys.executable, '-c', CHILD, '--config-path', str(config), '--data-dir', str(data),
        '--port', '0', '--no-shell', '--accept-agreement',
    ], cwd=Path(__file__).resolve().parents[2], env=environment,
       capture_output=True, text=True, encoding='utf-8', timeout=45)
    log = result.stdout + '\n' + result.stderr
    (tmp_path / 'startup.log').write_text(log, encoding='utf-8')
    assert result.returncode == 0, log
    assert 'YUELI_READY=1' in log, log
    assert 'FAKE_EMBEDDING_STARTED=1' in log, log
    assert 'EMOJI_BACKFILL_PENDING=1' in log, log
    assert 'CONTROLLED_SHUTDOWN=1' in log, log
    events = [json.loads(line) for line in log.splitlines() if line.startswith('{')]
    registrations = [event for event in events if event.get('event') == '首次登记向量空间']
    assert {event['consumer'] for event in registrations} == {'facts', 'knowledge', 'emoji'}, log
    emoji_space = next(event['space'] for event in registrations if event['consumer'] == 'emoji')
    expected_recipe = 'image+tags/v1' if slot == 'multimodal_embedding' else 'tags'
    assert f"recipe='{expected_recipe}'" in emoji_space, log
    bodies = [json.loads(line.split('=', 1)[1]) for line in log.splitlines() if line.startswith('FAKE_EMBEDDING_BODY=')]
    contents = [content for body in bodies for content in body['input']['contents']]
    assert contents and all(content['text'] == '开心' for content in contents), log
    assert any('image' in content for content in contents) == (slot == 'multimodal_embedding'), log
