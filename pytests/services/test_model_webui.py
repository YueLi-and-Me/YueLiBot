"""WebUI 模型工作台的配置读写与上游探测回归。"""

from __future__ import annotations

from pathlib import Path
import json
import re
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.core.config import model_webui


@pytest.fixture()
def config_copy(tmp_path: Path) -> Path:
    source = Path('config')
    for name in ('providers.toml', 'models.toml', 'bot.toml', 'features.toml'):
        shutil.copy2(source / name, tmp_path / name)
    return tmp_path


def test_snapshot_and_save_round_trip(config_copy: Path) -> None:
    snapshot = model_webui.snapshot(config_copy)
    assert snapshot['providers']
    assert snapshot['models']
    assert snapshot['tasks']['chat']['model_list']

    result = model_webui.save(config_copy, snapshot)
    assert result['ok'] is True

    again = model_webui.snapshot(config_copy)
    assert [item['name'] for item in again['providers']] == [
        item['name'] for item in snapshot['providers']
    ]
    assert again['models'] == snapshot['models']


def test_save_round_trips_balance_strategy(config_copy: Path) -> None:
    snapshot = model_webui.snapshot(config_copy)
    snapshot['tasks']['chat']['selection_strategy'] = 'balance'

    result = model_webui.save(config_copy, snapshot)

    assert result['ok'] is True
    again = model_webui.snapshot(config_copy)
    assert again['tasks']['chat']['selection_strategy'] == 'balance'


@pytest.mark.parametrize('enabled', [True, False])
def test_save_round_trips_model_thinking_switch(
    config_copy: Path,
    enabled: bool,
) -> None:
    snapshot = model_webui.snapshot(config_copy)
    snapshot['models'][0]['extra_body']['enable_thinking'] = enabled

    result = model_webui.save(config_copy, snapshot)

    assert result['ok'] is True
    again = model_webui.snapshot(config_copy)
    assert again['models'][0]['extra_body']['enable_thinking'] is enabled


def test_save_updates_vision_enabled_without_touching_providers(config_copy: Path) -> None:
    snapshot = model_webui.snapshot(config_copy)
    snapshot['vision_enabled'] = not snapshot['vision_enabled']
    snapshot['chat_image_enabled'] = not snapshot['chat_image_enabled']
    result = model_webui.save(config_copy, snapshot)
    assert result['ok'] is True
    again = model_webui.snapshot(config_copy)
    assert again['vision_enabled'] == snapshot['vision_enabled']
    assert again['chat_image_enabled'] == snapshot['chat_image_enabled']


def test_save_handles_empty_providers_and_models(config_copy: Path) -> None:
    snapshot = model_webui.snapshot(config_copy)
    snapshot['providers'] = []
    snapshot['models'] = []
    snapshot['vision_enabled'] = False
    snapshot['chat_image_enabled'] = False
    features = (config_copy / 'features.toml').read_text(encoding='utf-8')
    lines = features.splitlines()
    in_vector = False
    for line_index, line in enumerate(lines):
        if line.startswith('['):
            in_vector = line.split('#', 1)[0].strip() == '[vector]'
            continue
        if in_vector and line.split('#', 1)[0].strip().startswith('enabled'):
            lines[line_index] = 'enabled = false'
            break
    features = chr(10).join(lines) + chr(10)
    (config_copy / 'features.toml').write_text(features, encoding='utf-8')
    for task in snapshot['tasks']:
        snapshot['tasks'][task]['model_list'] = []

    result = model_webui.save(config_copy, snapshot)
    assert result['ok'] is True
    saved = model_webui.snapshot(config_copy)
    assert saved['providers'] == []
    assert saved['models'] == []


def test_save_reuses_blank_api_key(config_copy: Path) -> None:
    snapshot = model_webui.snapshot(config_copy)
    old_key = snapshot['providers'][0]['api_key']
    snapshot['providers'][0]['api_key'] = ''

    result = model_webui.save(config_copy, snapshot)
    assert result['ok'] is True
    saved = model_webui.snapshot(config_copy)
    assert saved['providers'][0]['api_key'] == old_key


class _ProviderHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == '/':
            self._send(200, {'ok': True})
            return
        if self.path == '/models':
            if self.headers.get('Authorization') == 'Bearer sk-test':
                self._send(200, {'data': [{'id': 'm1', 'name': 'Model One'}]})
            else:
                self._send(401, {'error': 'bad key'})
            return
        self._send(404, {'error': 'not found'})

    def _send(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


@pytest.mark.asyncio
async def test_connection_and_model_list_probe() -> None:
    server = ThreadingHTTPServer(('127.0.0.1', 0), _ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f'http://127.0.0.1:{server.server_port}'
    try:
        result = await model_webui.test_connection(
            base_url=base_url,
            api_key='sk-test',
            client_type='openai',
            auth_type='bearer',
            auth_name='',
            model_list_endpoint='/models',
        )
        assert result['network_ok'] is True
        assert result['api_key_valid'] is True

        models = await model_webui.list_models(
            base_url=base_url,
            api_key='sk-test',
            client_type='openai',
            auth_type='bearer',
            auth_name='',
            model_list_endpoint='/models',
        )
        assert [item['id'] for item in models] == ['m1']
    finally:
        server.shutdown()
        thread.join(timeout=2)
