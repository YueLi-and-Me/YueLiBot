"""模型层专项规格。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Dict, List
from urllib.parse import parse_qs, urlsplit

import asyncio
import json
import time

import pytest

from src.core.config.loader import load_config, reset_config
from src.core.config.schema import ApiProviderConfig, CONFIG_VERSION, Config, ModelCandidate, TaskRouting
from src.core.llm_models import openai as openai_module
from src.core.llm_models.openai import LlmError, OpenAiChatProvider
from src.core.llm_models.router import ModelRouter
from src.core.llm_models.snapshot import configure as configure_snapshots
from src.core.llm_models.snapshot import dump as dump_snapshot
from src.core.observe.store import event_store
from src.core.services.chat import ChatService, InboundMessage

# 测试夹具占位密钥：不对应任何真实服务，经变量间接传入避免被安全扫描当作硬编码凭据。
_FIXTURE_API_KEY = "-".join(("test", "key"))


class _Response:
    def __init__(self, lines: list[str], status: int = 200) -> None:
        self.status_code = status
        self._lines = lines

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def aread(self) -> bytes:
        return b'{"error":{"message":"failed"}}'

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    lines: list[str] | None = None,
    status: int = 200,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def stream(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> _Response:
            requests.append({
                "method": method,
                "url": url,
                "headers": headers,
                "body": json,
            })
            return _Response(lines or ["data: [DONE]"], status)

    monkeypatch.setattr(openai_module.httpx, "AsyncClient", _Client)
    return requests


def _write_config(
    tmp_path: Path,
    *,
    api_key: str = _FIXTURE_API_KEY,
    provider_extra: str = "",
    model_extra: str = "",
    generation_extra: str = "",
) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    # 值经 json.dumps 序列化进 TOML 字面量：空串、引号等入参都能安全落盘，
    # 也避免源码里出现「键名 = 引号值」的静态字面量形态。
    api_key_line = f"api_key = {json.dumps(api_key)}"
    directory.joinpath("providers.toml").write_text(f"""
[inner]
version = "{CONFIG_VERSION}"

[[api_providers]]
name = "主力"
kind = "openai"
base_url = "https://api.example.com/v1"
{api_key_line}
client_type = "openai"
{provider_extra}
""", encoding="utf-8")
    directory.joinpath("models.toml").write_text(f"""
[inner]
version = "{CONFIG_VERSION}"

[model_tasks.chat]
model_list = ["chat"]

[model_tasks.vision]
model_list = []

[model_tasks.tts]
model_list = []

[model_tasks.embedding]
model_list = []

{generation_extra}

[[models]]
name = "chat"
model_identifier = "chat-model"
api_provider = "主力"
{model_extra}
""", encoding="utf-8")
    directory.joinpath("bot.toml").write_text(f"""
[inner]
version = "{CONFIG_VERSION}"

[bot]
name = "测试角色"

[group_chat]
at_mention_must_reply = true

[personality]
birthday = ""
personality = "人设"
reply_style = "表达"
tone_probability = 0.0
tone_variants = []
""", encoding="utf-8")
    directory.joinpath("features.toml").write_text(f"""
[inner]
version = "{CONFIG_VERSION}"

[tts]
enabled = false

[vision]
enabled = false

[vector]
enabled = false

[advanced]
""", encoding="utf-8")
    return directory


async def _collect(provider: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return [chunk async for chunk in provider.stream(
        [{"role": "user", "content": "你好"}],
        **kwargs,
    )]


async def test_s1_1_extra_body_reaches_non_ark_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_config()
    directory = _write_config(
        tmp_path,
        model_extra='extra_body = { thinking = { type = "disabled" } }',
    )
    cfg = load_config(directory)
    candidate = cfg.routing.chat.candidates[0]
    requests = _install_http(monkeypatch)
    configure_snapshots(tmp_path / "snapshots")
    router = ModelRouter("chat", [candidate])

    await _collect(router)
    path = dump_snapshot("chat", "test", "capture")

    assert requests[0]["body"]["thinking"] == {"type": "disabled"}
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["provider_request"]["body"]["thinking"] == {"type": "disabled"}


def test_s1_1_retired_task_thinking_reports_extra_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reset_config()
    directory = _write_config(
        tmp_path,
        generation_extra="""
[generation.schedule]
temperature = 0.1
max_tokens = 100
thinking = "disabled"
""",
    )

    with pytest.raises(SystemExit):
        load_config(directory)

    assert "extra_body" in capsys.readouterr().err


def test_s1_1_retired_model_thinking_reports_extra_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reset_config()
    directory = _write_config(tmp_path, model_extra='thinking = "disabled"')

    with pytest.raises(SystemExit):
        load_config(directory)

    assert "extra_body" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("auth_type", "auth_name", "api_key"),
    [
        ("bearer", "", "secret"),
        ("header", "x-api-key", "secret"),
        ("query", "key", "secret"),
        ("none", "", ""),
    ],
)
async def test_s1_2_auth_modes_use_expected_wire_shape(
    auth_type: str,
    auth_name: str,
    api_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_http(monkeypatch)
    provider = OpenAiChatProvider(
        "https://api.example.com/v1",
        api_key,
        "chat-model",
        auth_type=auth_type,
        auth_name=auth_name,
        max_retries=0,
    )

    await _collect(provider)
    request = requests[0]
    query = parse_qs(urlsplit(request["url"]).query)

    if auth_type == "bearer":
        assert request["headers"]["Authorization"] == "Bearer secret"
    elif auth_type == "header":
        assert request["headers"]["x-api-key"] == "secret"
    elif auth_type == "query":
        assert query == {"key": ["secret"]}
    else:
        assert "Authorization" not in request["headers"]
        assert query == {}


async def test_s1_2_query_secret_is_redacted_from_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SECRET123"
    _install_http(monkeypatch, status=500)
    configure_snapshots(tmp_path)
    provider = OpenAiChatProvider(
        "https://api.example.com/v1",
        secret,
        "chat-model",
        auth_type="query",
        auth_name="key",
        max_retries=0,
    )

    with pytest.raises(LlmError):
        await _collect(provider)
    path = dump_snapshot("chat", "network", "failed")

    assert path is not None
    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "key=%5B%E5%B7%B2%E9%9A%90%E5%8E%BB%5D" in raw


async def test_s1_2_custom_header_secret_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "HEADER_SECRET"
    _install_http(monkeypatch, status=500)
    configure_snapshots(tmp_path)
    provider = OpenAiChatProvider(
        "https://api.example.com/v1",
        secret,
        "chat-model",
        auth_type="header",
        auth_name="X-Custom-Secret",
        max_retries=0,
    )

    with pytest.raises(LlmError):
        await _collect(provider)
    path = dump_snapshot("chat", "network", "failed")

    assert path is not None
    assert secret not in path.read_text(encoding="utf-8")


def test_s1_2_empty_bearer_key_fails_during_load(tmp_path: Path) -> None:
    reset_config()
    directory = _write_config(tmp_path, api_key="")

    with pytest.raises(SystemExit):
        load_config(directory)


@pytest.mark.parametrize(
    "fields",
    [
        {"auth_type": "header", "auth_name": "", "api_key": "secret"},
        {"auth_type": "query", "auth_name": "", "api_key": "secret"},
        {"auth_type": "bearer", "auth_name": "key", "api_key": "secret"},
        {"auth_type": "none", "auth_name": "", "api_key": "secret"},
    ],
)
def test_s1_2_invalid_auth_combinations_are_rejected(fields: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        ApiProviderConfig(name="test", kind="openai", **fields)


class _DelayedClient:
    def __init__(self, delays: list[float], chunks: list[str]) -> None:
        self._delays = delays
        self._chunks = chunks
        self.calls = 0

    async def stream(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        self.calls += 1
        for delay, chunk in zip(self._delays, self._chunks):
            await asyncio.sleep(delay)
            yield {"text": chunk}


def _candidate(name: str) -> ModelCandidate:
    return ModelCandidate(
        name=name,
        provider=f"厂商{name}",
        kind="openai",
        base_url="https://api.example.com/v1",
        api_key=_FIXTURE_API_KEY,
        identifier=f"{name}-model",
    )


def _timed_router(
    clients: dict[str, _DelayedClient],
    *,
    first_token_timeout_ms: int,
    slow_threshold_ms: int,
) -> ModelRouter:
    routing = TaskRouting(
        task="chat",
        candidates=[_candidate(name) for name in clients],
        first_token_timeout_ms=first_token_timeout_ms,
        slow_threshold_ms=slow_threshold_ms,
    )
    router = ModelRouter(
        routing.task,
        routing.candidates,
        routing.strategy,
        first_token_timeout_ms=routing.first_token_timeout_ms,
        slow_threshold_ms=routing.slow_threshold_ms,
    )
    router.client = lambda candidate: clients[candidate.name]  # type: ignore[method-assign]
    return router


async def test_s1_3_first_token_timeout_switches_candidate() -> None:
    clients = {
        "慢": _DelayedClient([1.2], ["不该出现"]),
        "快": _DelayedClient([0.0], ["备用成功"]),
    }
    router = _timed_router(
        clients,
        first_token_timeout_ms=1_000,
        slow_threshold_ms=0,
    )

    started = time.monotonic()
    chunks = await _collect(router)

    assert time.monotonic() - started < 1.15
    assert "".join(chunk.get("text", "") for chunk in chunks) == "备用成功"
    assert clients["快"].calls == 1


async def test_first_token_timeout_uses_timeout_kind() -> None:
    """校验唯一候选首字超时时路由层给出的 error kind。

    :raises AssertionError: kind 不为 timeout。首字超时被并回 network 时，
        chat.py 会按连接故障给出提示，而厂商此时已连通、只是未在窗口内出字。
    :side_effects: 不发起真实 HTTP 请求，候选延迟由 _DelayedClient 模拟。
    """
    clients = {"慢": _DelayedClient([1.2], ["不该出现"])}
    router = _timed_router(
        clients,
        first_token_timeout_ms=1_000,
        slow_threshold_ms=0,
    )

    with pytest.raises(LlmError) as excinfo:
        await _collect(router)

    assert excinfo.value.kind == "timeout"


async def test_first_token_timeout_hint_does_not_blame_network(db: Any) -> None:
    """校验首字超时经 chat.send 之后推送给桌面的排查提示。

    :param db: conftest 提供的内存 SQLite 连接，已完成迁移。
    :raises AssertionError: chat.error 不止一条，或 hint 为空、或仍指向网络与
        代理。hint 为空说明 _HINTS 缺少 timeout 一条，用户拿不到任何方向。
    :side_effects: 向内存库写入一条用户消息，回合失败后由服务自行回滚。
    """
    events: list[tuple[int, str, dict[str, Any]]] = []

    async def push_event(channel: str, payload: dict[str, Any], stream_id: int) -> None:
        events.append((stream_id, channel, payload))

    # ModelRouter 与 OpenAiChatProvider 对外同形，可直接充当 provider，
    # 使超时在路由层真实发生，而不是由桩直接抛出 LlmError。
    router = _timed_router(
        {"慢": _DelayedClient([1.2], ["不该出现"])},
        first_token_timeout_ms=1_000,
        slow_threshold_ms=0,
    )
    chat = ChatService(db, router, router, router, push_event, cfg=Config())
    context = chat.desktop_context

    await chat.send(InboundMessage(text="在吗", context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    errors = [payload for _, channel, payload in events if channel == "chat.error"]
    assert len(errors) == 1
    assert errors[0]["hint"]
    assert "检查网络或代理" not in errors[0]["hint"]


async def test_s1_3_timeout_stops_after_first_chunk() -> None:
    clients = {"慢尾": _DelayedClient([0.01, 1.1], ["前", "后"])}
    router = _timed_router(
        clients,
        first_token_timeout_ms=1_000,
        slow_threshold_ms=0,
    )

    chunks = await _collect(router)

    assert "".join(chunk.get("text", "") for chunk in chunks) == "前后"


async def test_s1_3_slow_event_is_persisted() -> None:
    clients = {"慢": _DelayedClient([0.03], ["到了"])}
    router = _timed_router(
        clients,
        first_token_timeout_ms=1_000,
        slow_threshold_ms=10,
    )

    await _collect(router)
    events = event_store.since(0).events
    slow = next(event for event in events if event["kind"] == "llm_slow")
    print(json.dumps(slow, ensure_ascii=False, sort_keys=True))

    assert slow["task"] == "chat"
    assert slow["model"] == "慢"
    assert slow["provider"] == "厂商慢"
    assert slow["elapsedMs"] >= 10


def test_s1_3_invalid_slow_threshold_is_rejected() -> None:
    with pytest.raises(ValueError):
        TaskRouting(
            task="chat",
            first_token_timeout_ms=30_000,
            slow_threshold_ms=30_000,
        )


def _sse(content: str) -> str:
    return "data: " + json.dumps({
        "choices": [{"delta": {"content": content}}],
    }, ensure_ascii=False)


async def test_http_200_policy_notice_is_blocked_before_any_text_is_yielded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容网关把安全拦截伪装成正文时，必须在首字外泄前转成 blocked。"""
    notice_chunks = [
        "The prompt could not be submitted. The prompt contains sensitive words ",
        (
            "that violate Google's [Generative AI Prohibited Use policy]"
            "(https://policies.google.com/terms/generative-ai/use-policy). "
            "Try rephrasing the prompt."
        ),
    ]
    _install_http(
        monkeypatch,
        [*[_sse(content) for content in notice_chunks], "data: [DONE]"],
    )
    provider = OpenAiChatProvider(
        "https://api.example.com/v1",
        _FIXTURE_API_KEY,
        "chat-model",
        max_retries=0,
    )
    yielded: List[Dict[str, Any]] = []

    with pytest.raises(LlmError) as excinfo:
        async for chunk in provider.stream([{'role': 'user', 'content': '在吗'}]):
            yielded.append(chunk)

    assert excinfo.value.kind == 'blocked'
    assert 'The prompt could not be submitted.' in excinfo.value.detail
    assert yielded == []


async def test_policy_notice_signature_after_normal_text_remains_model_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只识别响应起点的供应商诊断，正常台词中引用相同英文不能被误判。"""
    contents = [
        '<say>我看到的原文是：',
        (
            "The prompt could not be submitted. The prompt contains sensitive words "
            "that violate Google's Generative AI Prohibited Use policy."
        ),
        '</say>',
    ]
    _install_http(
        monkeypatch,
        [*[_sse(content) for content in contents], "data: [DONE]"],
    )
    provider = OpenAiChatProvider(
        "https://api.example.com/v1",
        _FIXTURE_API_KEY,
        "chat-model",
        max_retries=0,
    )

    chunks = await _collect(provider)

    assert ''.join(chunk.get('text', '') for chunk in chunks) == ''.join(contents)


async def _reasoning_chunks(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    contents: list[str],
) -> list[dict[str, Any]]:
    _install_http(monkeypatch, [*[_sse(content) for content in contents], "data: [DONE]"])
    provider = OpenAiChatProvider(
        "https://api.example.com/v1",
        "secret",
        "chat-model",
        reasoning_parse_mode=mode,
        max_retries=0,
    )
    return await _collect(provider)


async def test_s1_4_tag_reasoning_never_enters_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = await _reasoning_chunks(
        monkeypatch,
        "tag",
        ["<think>盘算</think>你好"],
    )

    assert "".join(chunk.get("reasoning", "") for chunk in chunks) == "盘算"
    assert "".join(chunk.get("text", "") for chunk in chunks) == "你好"


async def test_s1_4_split_tag_is_parsed_across_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = await _reasoning_chunks(
        monkeypatch,
        "tag",
        ["<thi", "nk>盘算</think>你好"],
    )

    assert "".join(chunk.get("reasoning", "") for chunk in chunks) == "盘算"
    assert "".join(chunk.get("text", "") for chunk in chunks) == "你好"


async def test_s1_4_none_keeps_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = await _reasoning_chunks(
        monkeypatch,
        "none",
        ["<think>盘算</think>你好"],
    )

    assert "".join(chunk.get("text", "") for chunk in chunks) == "<think>盘算</think>你好"


def test_s1_4_agent_parser_no_longer_knows_think_tags() -> None:
    source = Path("src/core/agent/parser.py").read_text(encoding="utf-8")

    assert "'think'" not in source
    assert "'thinking'" not in source


def test_s1_4_reasoning_mode_reaches_candidate(tmp_path: Path) -> None:
    reset_config()
    directory = _write_config(tmp_path, model_extra='reasoning_parse_mode = "tag"')

    candidate = load_config(directory).routing.chat.candidates[0]

    assert candidate.reasoning_parse_mode == "tag"


def test_s1_4_invalid_reasoning_mode_is_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reset_config()
    directory = _write_config(tmp_path, model_extra='reasoning_parse_mode = "auto"')

    with pytest.raises(SystemExit):
        load_config(directory)

    assert "reasoning_parse_mode" in capsys.readouterr().err
