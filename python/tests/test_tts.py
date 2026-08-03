"""TTS 链路与豆包语音协议回归。

两条历史断链：
  · 正常对话完全不发声——chat.py 的 _track_speech 是个空 stub，_speak_audio
    全仓库只在 speak()（主动搭话）里被调用；
  · 而 speak() 里写的是 asyncio.create_task(self._speak_audio(...))，注入进来的
    TtsService.speak 却是同步函数，等于 create_task(None)，直接抛 TypeError。
    也就是说主动搭话那条其实同样是坏的。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from typing import Any, AsyncIterator, List, Tuple

import pytest

from yueli.config.schema import Config
from yueli.memory.store import MemoryStore
from yueli.services import tts as tts_module
from yueli.services.chat import ChatService
from yueli.services.tts import TtsService


# ── 假 provider / 假 HTTP ──────────────────────────────────────────────

class _Streamer:
    model = 'fake'

    def __init__(self, chunks: List[str]) -> None:
        self._chunks = chunks

    async def stream(self, messages, temperature=0.85, max_tokens=None, signal=None) -> AsyncIterator[dict]:
        for chunk in self._chunks:
            yield {'text': chunk}


async def _noop_push(_channel: str, _payload: Any) -> None:
    return None


class _FakeResponse:
    def __init__(self, payload: dict | None, status: int = 200, raw: bytes = b'') -> None:
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload) if payload is not None else ''
        self.content = raw

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        return self._payload or {}


class _FakeClient:
    """记录最后一次请求，供断言请求形状。"""

    captured: dict = {}
    response: _FakeResponse = _FakeResponse({'code': 3000, 'data': ''})

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    async def __aenter__(self) -> '_FakeClient':
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False

    async def post(self, url: str, headers: dict | None = None, json: dict | None = None) -> _FakeResponse:
        _FakeClient.captured = {'url': url, 'headers': headers or {}, 'json': json or {}}
        return _FakeClient.response


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tts_module.httpx, 'AsyncClient', _FakeClient)
    _FakeClient.captured = {}
    return _FakeClient


def _volc_cfg(**overrides: Any) -> Config:
    cfg = Config()
    cfg.tts.enabled = True
    cfg.tts.client_type = 'volcengine'
    cfg.tts.app_id = 'my-app-id'
    cfg.tts.api_key = 'my-access-token'
    cfg.tts.voice = 'zh_female_test'
    cfg.tts.cluster = 'volcano_tts'
    for key, value in overrides.items():
        setattr(cfg.tts, key, value)
    return cfg


# ── 豆包语音协议 ───────────────────────────────────────────────────────

async def test_volcengine_request_shape(fake_http) -> None:
    audio = b'\x00\x01fake-mp3'
    fake_http.response = _FakeResponse({'code': 3000, 'data': base64.b64encode(audio).decode()})

    data = await TtsService(_volc_cfg(), _noop_push)._synth('你好呀')

    assert data == audio, '应把 data 字段 base64 解码后作为音频返回'

    sent = fake_http.captured
    assert sent['url'] == 'https://openspeech.bytedance.com/api/v1/tts'
    # ★ 分号，不是空格。写成 "Bearer {token}" 会 401。
    assert sent['headers']['Authorization'] == 'Bearer;my-access-token'

    body = sent['json']
    assert body['app'] == {'appid': 'my-app-id', 'token': 'my-access-token', 'cluster': 'volcano_tts'}
    assert body['audio']['voice_type'] == 'zh_female_test'
    assert body['request']['text'] == '你好呀'
    assert body['request']['operation'] == 'query'
    assert body['request']['reqid'], 'reqid 必填且每次不同'


async def test_volcengine_reqid_is_unique_per_call(fake_http) -> None:
    fake_http.response = _FakeResponse({'code': 3000, 'data': base64.b64encode(b'x').decode()})
    service = TtsService(_volc_cfg(), _noop_push)

    await service._synth('第一句')
    first = fake_http.captured['json']['request']['reqid']
    await service._synth('第二句')
    second = fake_http.captured['json']['request']['reqid']

    assert first != second


async def test_volcengine_surfaces_api_error_code(fake_http) -> None:
    """字段对不上时必须能从日志看出是哪里的问题，不能静默失败。"""
    fake_http.response = _FakeResponse({'code': 3001, 'message': 'invalid cluster'})

    with pytest.raises(RuntimeError) as excinfo:
        await TtsService(_volc_cfg(), _noop_push)._synth('测试')

    assert '3001' in str(excinfo.value)
    assert 'invalid cluster' in str(excinfo.value)


async def test_openai_path_unchanged(fake_http) -> None:
    """默认仍走 OpenAI 兼容协议，请求形状不能被改动影响。"""
    fake_http.response = _FakeResponse(None, raw=b'mp3-bytes')
    cfg = Config()
    cfg.tts.enabled = True
    cfg.tts.base_url = 'https://api.example.com/v1'
    cfg.tts.api_key = 'sk-test'
    cfg.tts.model = 'tts-1'
    cfg.tts.voice = 'alloy'

    data = await TtsService(cfg, _noop_push)._synth('你好')

    assert data == b'mp3-bytes'
    sent = fake_http.captured
    assert sent['url'] == 'https://api.example.com/v1/audio/speech'
    assert sent['headers']['Authorization'] == 'Bearer sk-test'
    assert sent['json']['input'] == '你好'


def test_ready_requirements_differ_by_protocol() -> None:
    # 豆包语音不看 model / base_url，但 appid 和 token 缺一不可
    assert _volc_cfg().tts.ready is True
    assert _volc_cfg(app_id='').tts.ready is False
    assert _volc_cfg(api_key='').tts.ready is False
    assert _volc_cfg(voice='').tts.ready is False

    openai_cfg = Config()
    openai_cfg.tts.enabled = True
    openai_cfg.tts.base_url = 'https://x/v1'
    openai_cfg.tts.voice = 'alloy'
    assert openai_cfg.tts.ready is False, 'OpenAI 协议下 model 必填'
    openai_cfg.tts.model = 'tts-1'
    assert openai_cfg.tts.ready is True


# ── 断链修复 ───────────────────────────────────────────────────────────

def _chat(db, spoken: List[Tuple[str, int]]) -> ChatService:
    def sync_speak(text: str, turn: int) -> None:      # TtsService.speak 的真实形态：同步
        spoken.append((text, turn))

    chat = ChatService(db=db, provider=_Streamer(['<say emotion="smile">你好呀</say>']),
                       push_event=_noop_push)
    chat._speak_audio = sync_speak
    return chat


async def test_normal_reply_now_speaks(db) -> None:
    """正常对话此前完全不发声——_track_speech 是空 stub。"""
    spoken: List[Tuple[str, int]] = []
    chat = _chat(db, spoken)

    await chat.send('在吗')
    await chat._inflight

    assert spoken, '正常对话必须触发语音合成'
    assert spoken[0][0] == '你好呀', f'送去合成的应是台词本身，实际：{spoken}'


async def test_multiple_say_blocks_speak_separately(db) -> None:
    """按 <say> 分句送，而不是等整段收完——开口延迟才跟字幕对得上。"""
    spoken: List[Tuple[str, int]] = []
    chat = ChatService(
        db=db,
        provider=_Streamer(['<say>第一句</say>', '<say>第二句</say>']),
        push_event=_noop_push,
    )
    chat._speak_audio = lambda text, turn: spoken.append((text, turn))

    await chat.send('说两句')
    await chat._inflight

    assert [s[0] for s in spoken] == ['第一句', '第二句']


async def test_proactive_speak_no_longer_raises_on_sync_callback(db) -> None:
    """回归：speak() 里的 create_task(sync_fn(...)) 等于 create_task(None)。"""
    spoken: List[Tuple[str, int]] = []
    chat = _chat(db, spoken)

    turn = chat.speak([{'emotion': 'smile', 'text': '你还在啊'}])

    assert spoken == [('你还在啊', turn)]


async def test_awaitable_callback_is_also_supported(db) -> None:
    """注入协程函数时同样要能工作，不能只兼容同步。"""
    spoken: List[str] = []

    async def async_speak(text: str, _turn: int) -> None:
        spoken.append(text)

    chat = ChatService(db=db, provider=_Streamer(['<say>好</say>']), push_event=_noop_push)
    chat._speak_audio = async_speak

    await chat.send('嗯')
    await chat._inflight
    import asyncio
    await asyncio.sleep(0.02)   # 让 create_task 起的协程跑完

    assert spoken == ['好']


async def test_interrupt_clears_buffer_and_cancels_audio(db) -> None:
    """打断后不能把上一句的尾巴念进下一轮。"""
    cancelled: List[int] = []
    chat = ChatService(db=db, provider=_Streamer(['<say>半截']), push_event=_noop_push)
    chat._speak_audio = lambda text, turn: None
    chat._cancel_audio = lambda turn: cancelled.append(turn)

    await chat.send('第一句')
    await chat._inflight
    chat.interrupt()

    assert chat._speech_buffer == []
    assert cancelled, '打断时应叫停已经在播的音频'


# ── 加载期防呆 ─────────────────────────────────────────────────────────

def test_volcengine_provider_rejected_on_non_tts_task(tmp_path) -> None:
    """豆包语音是私有协议，指到 chat 上只会在运行时抛难定位的错，应在加载期拦下。"""
    from yueli.config.loader import _load_split_config
    # 复用 split-config 的完整夹具，再只覆盖 providers/models 两份——
    # 这样 bot.toml / features.toml 的必填字段变化会自动跟上，不用在这儿维护一份。
    from tests.test_split_config import _write_split_config

    directory = tmp_path / 'config'
    _write_split_config(directory)
    directory.joinpath('providers.toml').write_text("""
[inner]
version = "1.0.0"

[[api_providers]]
name = "voice"
kind = "volcengine"
base_url = "https://openspeech.bytedance.com"
api_key = "token"
client_type = "volcengine"
app_id = "appid"
""", encoding='utf-8')
    directory.joinpath('models.toml').write_text("""
[inner]
version = "1.0.0"

[model_tasks]
chat = "m"
vision = "m"
tts = "m"
embedding = "m"

[[models]]
name = "m"
model_identifier = "x"
api_provider = "voice"
""", encoding='utf-8')

    with pytest.raises(ValueError) as excinfo:
        _load_split_config(directory)

    assert 'model_tasks.chat' in str(excinfo.value)
    assert '只支持 tts' in str(excinfo.value)
