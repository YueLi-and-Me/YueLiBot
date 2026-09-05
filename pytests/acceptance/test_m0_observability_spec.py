"""验证观测阶段标识、前端读取方式和持久化快照的结构契约。

本模块通过源码检查和临时数据库验证后端事件记录、主动任务阶段登记、
前端按稳定标识读取状态以及历史事件可重放等约束。
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import asyncio
import json
import sqlite3
import threading
import time

import pytest

from src.core.config.schema import ModelCandidate
from src.core.llm_models.openai import LlmError
from src.core.llm_models.router import ModelRouter


ROOT = Path(__file__).resolve().parents[2]


class _SnapshotClient:
    def __init__(self, snapshot: Any, error: LlmError, extra_body: dict | None = None) -> None:
        self._snapshot = snapshot
        self._error = error
        self._extra_body = extra_body or {}

    async def stream(
        self,
        messages: list[dict],
        temperature: float = 0.85,
        max_tokens: int | None = None,
        signal: asyncio.Event | None = None,
        response_format: dict[str, str] | None = None,
    ):
        body: dict[str, Any] = {
            "model": "compat",
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            **self._extra_body,
        }
        self._snapshot.record_provider_request(
            "https://example.test/chat/completions",
            {},
            body,
            candidate=self._snapshot.current_candidate(),
        )
        raise self._error
        yield


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _webui_source() -> str:
    """把 WebUI 前端全部源码拼成一段文本，供跨文件的结构约束检查使用。

    :return: webui/src 下所有 .ts/.tsx 文件内容的拼接结果。
    """
    files = sorted(
        [*(ROOT / "webui/src").rglob("*.ts"), *(ROOT / "webui/src").rglob("*.tsx")]
    )
    assert files, "webui/src 下没有找到前端源码"
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


def _module(name: str) -> Any:
    try:
        return import_module(name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"缺少规格要求的模块 {name}：{exc}")


def _new_store(path: Path, *, count: int = 20_000, hours: int = 72) -> Any:
    store_module = _module("src.core.observe.store")
    ledger = store_module.EventStore()
    ledger.configure(path, retention_count=count, retention_hours=hours)
    return ledger


def test_star_01_stage_ids_are_frozen() -> None:
    stages = _module("src.core.observe.stages")
    assert {stage.id for stage in stages.STAGES} == {
        "received",
        "gated",
        "context",
        "expression",
        "generating",
        "dispatching",
        "replied",
        "failed",
    }


def test_star_02_proactive_registers_full_stage_sequence() -> None:
    source = _source("src/core/services/proactive.py")
    assert "enter_stage" in source, "主动搭话仍未登记任何阶段"
    for stage_name in ("GENERATING", "DISPATCHING", "REPLIED"):
        assert stage_name in source, f"主动搭话缺少 {stage_name} 阶段"


def test_desktop_dispatches_before_marking_replied() -> None:
    source = _source("src/core/services/chat/service.py")
    desktop_branch = source.index(
        "if context.stream.platform == 'desktop':",
        source.index("trace.emit('llm_final'"),
    )
    dispatching = source.index("self._mark_stage(context, DISPATCHING", desktop_branch)
    done = source.index("await self._emit(stream_id, 'chat.done'", desktop_branch)
    replied = source.index("context, REPLIED", done)
    assert desktop_branch < dispatching < done < replied


def test_star_03_frontends_do_not_compare_chinese_stage_labels() -> None:
    webui = _webui_source()
    for label in ("失败", "组织上下文"):
        assert f"entry.stage === '{label}'" not in webui


def test_star_04_events_survive_store_reconfigure(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    first = _new_store(db_path)
    first.append("user_input", "received", 1, 7, {"text": "你好"})
    first.append("llm_final", "generating", 1, 7, {"text": "在呢"})
    first.close()

    second = _new_store(db_path)
    page = second.since(0, 1_000)
    assert [entry["kind"] for entry in page.events] == ["user_input", "llm_final"]
    second.close()


def test_star_05_autoincrement_does_not_rewind_after_full_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_module = _module("src.core.observe.store")
    monkeypatch.setattr(store_module, "_CLEANUP_EVERY", 1)
    ledger = store_module.EventStore()
    ledger.configure(tmp_path / "memory.db", retention_count=20_000, retention_hours=0)
    old = [ledger.append("probe", "", None, None, {"index": index})["seq"] for index in range(3)]
    assert ledger.since(0, 1_000).events == []
    current = ledger.append("probe", "", None, None, {"index": 3})["seq"]
    assert current > max(old)
    ledger.close()


def test_retention_count_removes_oldest_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_module = _module("src.core.observe.store")
    monkeypatch.setattr(store_module, "_CLEANUP_EVERY", 1)
    ledger = store_module.EventStore()
    ledger.configure(tmp_path / "memory.db", retention_count=3, retention_hours=999_999)
    for index in range(5):
        ledger.append("probe", "", None, None, {"index": index})
    assert [entry["index"] for entry in ledger.since(0, 10).events] == [2, 3, 4]
    ledger.close()


def test_star_06_ledger_does_not_commit_business_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    business = sqlite3.connect(db_path)
    business.execute("CREATE TABLE business_rows (value TEXT NOT NULL)")
    business.commit()
    ledger = _new_store(db_path)

    business.execute("INSERT INTO business_rows (value) VALUES ('尚未提交')")
    result: dict[str, Any] = {}

    def write_event() -> None:
        result["event"] = ledger.append("probe", "context", 1, 9, {"ok": True})

    worker = threading.Thread(target=write_event)
    worker.start()
    time.sleep(0.05)
    business.rollback()
    worker.join(timeout=4)
    assert not worker.is_alive(), "独立账本连接未能在业务事务释放后完成写入"
    assert business.execute("SELECT COUNT(*) FROM business_rows").fetchone()[0] == 0
    assert result["event"]["kind"] == "probe"
    assert [entry["kind"] for entry in ledger.since(0, 10).events] == ["probe"]
    ledger.close()
    business.close()


def test_star_07_disconnect_replay_has_no_gaps_or_duplicates(tmp_path: Path) -> None:
    assert "@router.websocket('/ws/events')" in _source("src/core/api/ws.py")
    ledger = _new_store(tmp_path / "memory.db")
    before = ledger.append("user_input", "received", 1, 1, {"text": "第一轮"})["seq"]
    for turn in (2, 3):
        ledger.append("user_input", "received", 1, turn, {"text": f"第{turn}轮"})
        ledger.append("llm_final", "generating", 1, turn, {"text": "回复"})
    replay = ledger.since(before, 1_000).events
    seqs = [entry["seq"] for entry in replay]
    assert len(replay) == 4
    assert seqs == sorted(set(seqs))
    ledger.close()


@pytest.mark.asyncio
async def test_star_08_queue_overflow_forces_reconnect_and_store_recovers_all(
    tmp_path: Path,
) -> None:
    events = _module("src.core.observe.events")
    ledger = _new_store(tmp_path / "memory.db")
    broadcaster = events.EventBroadcaster(queue_size=2)
    subscriber = broadcaster.subscribe()
    persisted = []
    for index in range(10):
        entry = ledger.append("probe", "context", 1, index, {"index": index})
        persisted.append(entry)
        broadcaster.publish(entry)
    await asyncio.wait_for(subscriber.overflowed.wait(), timeout=1)
    replay = ledger.since(0, 100).events
    assert [entry["seq"] for entry in replay] == [entry["seq"] for entry in persisted]
    ledger.close()


def test_star_09_electron_observability_window_is_removed() -> None:
    assert "TRACE_POLL_MS" not in _webui_source()
    assert not (ROOT / "electron/renderer/observability.ts").exists()
    assert not (ROOT / "electron/renderer/observability.html").exists()


@pytest.mark.asyncio
async def test_star_10_snapshot_keeps_model_extra_body(tmp_path: Path) -> None:
    snapshot = _module("src.core.llm_models.snapshot")
    snapshot.configure(tmp_path)
    candidate = ModelCandidate(
        name="compat",
        provider="secondary",
        kind="openai",
        identifier="compat",
        extra_body={"thinking": {"type": "enabled"}},
    )
    router = ModelRouter("chat", [candidate])
    client = _SnapshotClient(
        snapshot,
        LlmError("network", "连接失败"),
        candidate.extra_body,
    )
    router.client = lambda selected: client  # type: ignore[method-assign]
    with pytest.raises(LlmError):
        async for _ in router.stream(
            [{"role": "user", "content": "你好"}],
            temperature=0.8,
            max_tokens=200,
        ):
            pass
    path = snapshot.dump("chat", "network", "连接失败")
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "thinking" not in payload["internal_request"]
    assert payload["provider_request"]["body"]["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_star_11_snapshot_records_all_candidate_attempts(tmp_path: Path) -> None:
    snapshot = _module("src.core.llm_models.snapshot")
    snapshot.configure(tmp_path)
    candidates = [
        ModelCandidate(
            name=f"model-{index}",
            provider=f"provider-{index}",
            kind="openai",
            identifier=f"model-{index}",
        )
        for index in range(3)
    ]
    clients = {
        candidate.name: _SnapshotClient(snapshot, LlmError("network", f"{candidate.name} 失败"))
        for candidate in candidates
    }
    router = ModelRouter("chat", candidates)
    router.client = lambda candidate: clients[candidate.name]  # type: ignore[method-assign]
    with pytest.raises(LlmError):
        async for _ in router.stream([], temperature=0.8):
            pass
    path = snapshot.dump("chat", "network", "全部失败")
    assert path is not None
    attempts = json.loads(path.read_text(encoding="utf-8"))["attempts"]
    assert [attempt["model"] for attempt in attempts] == ["model-0", "model-1", "model-2"]


def test_stage_and_snapshot_path_are_persisted(tmp_path: Path) -> None:
    events = _module("src.core.observe.events")
    stages = _module("src.core.observe.stages")
    ledger = _new_store(tmp_path / "memory.db")
    previous = events.event_store
    events.event_store = ledger
    try:
        stage_entry = events.enter_stage(stages.GENERATING, 1, "桌面", turn_id=7)
        snapshot_path = tmp_path / "failure.json"
        snapshot_path.write_text("{}", encoding="utf-8")
        error_entry = events.emit(
            "llm_error",
            streamId=1,
            turnId=7,
            snapshotPath=str(snapshot_path),
        )
    finally:
        events.event_store = previous
    assert stage_entry["kind"] == "stage"
    assert stage_entry["stage"] == "generating"
    assert Path(error_entry["snapshotPath"]).is_file()
    ledger.close()
