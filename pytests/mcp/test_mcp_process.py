"""实际启动宿主子进程，验证连不上的 MCP 服务不会阻塞就绪公告。

配置和数据全部在 pytest 临时目录，模型 HTTP 始终使用假传输，QQ 适配器不启动。
"""

from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tomllib

from src.core.config.bootstrap import render_example_configs
from src.core.config.schema import CONFIG_VERSION
from src.core.config.settings_webui import _write_documented_toml, file_schema
from src.core.mcp.config import render_mcp_config

CHILD = '''
from pathlib import Path
import asyncio
import httpx
import sys
import time
from src import main
from src.core.api.state import app_state
from src.core.config import adapter_selection, bootstrap
from src.core.mcp.compat.sdk import CONNECT_TIMEOUT_SECONDS

fixture_adapters = Path(sys.argv[sys.argv.index('--config-path') + 1]) / 'adapters'
adapter_selection.ADAPTERS_ROOT = fixture_adapters
bootstrap.ADAPTERS_ROOT = fixture_adapters
original_client = httpx.AsyncClient
async def fake_model(request):
    assert request.url.host == 'models.invalid', str(request.url)
    await asyncio.sleep(60)
    raise AssertionError('不允许访问真实模型')
httpx.AsyncClient = lambda **kw: original_client(transport=httpx.MockTransport(fake_model), **kw)

original_startup = main._ReadyAnnouncingServer.startup
async def startup(self, sockets=None):
    await original_startup(self, sockets)
    assert app_state.mcp._task is not None
    assert time.perf_counter() - main._init_started_at < CONNECT_TIMEOUT_SECONDS
    print('MCP_BACKGROUND_STARTED=1', flush=True)
    print('READY_BEFORE_MCP_TIMEOUT=1', flush=True)
    async def stop_after_mcp_failure():
        await app_state.mcp._task
        assert app_state.mcp.statuses()[0].state == '失败'
        print('MCP_UNREACHABLE=1', flush=True)
        self.should_exit = True
    asyncio.create_task(stop_after_mcp_failure())
main._ReadyAnnouncingServer.startup = startup
main.main()
print('CONTROLLED_SHUTDOWN=1', flush=True)
'''


def test_real_process_reaches_ready_with_unreachable_mcp(tmp_path: Path) -> None:
    """保留真实组合根、生命周期与监听，只把模型访问和关停时机设为受控假件。"""
    config, data = tmp_path / 'config', tmp_path / 'data'
    render_example_configs(config)
    repository = Path(__file__).resolve().parents[2]
    for manifest in (repository / 'adapters').glob('*/_manifest.json'):
        target = config / 'adapters' / manifest.parent.name / manifest.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(manifest, target)
    for path in (config / 'adapters').glob('*/config.toml'):
        path.write_text(path.read_text(encoding='utf-8').replace('enabled = true', 'enabled = false'), encoding='utf-8')
    for name in ('providers.toml', 'models.toml', 'features.toml', 'bot.toml'):
        path = config / name
        document = tomllib.loads(path.read_text(encoding='utf-8'))
        if name == 'providers.toml':
            for provider in document['api_providers']:
                provider.update(api_key='fake-process-key', base_url='https://models.invalid/v1')
        elif name == 'features.toml':
            document['telemetry']['enabled'] = False
            document['vision'].update(enabled=False, chat_image_enabled=False, chat_video_enabled=False)
            document['vector']['enabled'] = False
            document['memory_feedback']['enabled'] = False
        elif name == 'bot.toml':
            document['desktop_pet']['enabled'] = False
        _write_documented_toml(path, file_schema(name), document, CONFIG_VERSION)
    (config / 'mcp.toml').write_text(render_mcp_config({'mcp': {'enabled': True, 'servers': [{
        'name': 'unreachable', 'transport': 'http', 'url': 'http://127.0.0.1:9/mcp',
    }]}}), encoding='utf-8')
    result = subprocess.run([sys.executable, '-c', CHILD, '--config-path', str(config),
        '--data-dir', str(data), '--port', '0', '--no-shell', '--accept-agreement'],
        cwd=repository, env=dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8'),
        capture_output=True, text=True, encoding='utf-8', timeout=45)
    log = result.stdout + '\n' + result.stderr
    (tmp_path / 'startup.log').write_text(log, encoding='utf-8')
    assert result.returncode == 0, log
    for marker in ('YUELI_READY=1', 'MCP_BACKGROUND_STARTED=1', 'READY_BEFORE_MCP_TIMEOUT=1', 'MCP_UNREACHABLE=1', 'CONTROLLED_SHUTDOWN=1'):
        assert marker in log, log
    events = [json.loads(line) for line in log.splitlines() if line.startswith('{')]
    assert any('MCP 服务 unreachable 连接失败' in e.get('event', '') for e in events), '缺少不可达服务的失败诊断'
