"""验证观测账本、自检流程和模型请求快照的资源生命周期。

本模块属于后端观测与自检测试，覆盖事件账本未配置时的显式错误、临时账本的创建与关闭、
阶段事件持久化以及无可用模型时内部请求快照的记录。测试依赖 src.selftest、src.observe、
src.core.common.db.connection 和 src.core.llm_models 的实际实现，并通过 pytest 临时目录隔离数据库。
"""
from __future__ import annotations

from json import loads
from pathlib import Path

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.core.api.http import router as http_router
from src.core.common.db.connection import close_db
from src.core.config.schema import Config
from src.core.llm_models import snapshot
from src.core.llm_models.openai import LlmError
from src.core.llm_models.router import ModelRouter
from src import selftest
from src.core.observe import events
from src.core.observe.stages import GENERATING, label_for
from src.core.observe.store import event_store


ROOT = Path(__file__).resolve().parents[2]


def test_emit_requires_configured_ledger() -> None:
    event_store.close()
    with pytest.raises(RuntimeError, match="事件账本尚未配置"):
        events.emit("probe")


def test_selftest_configures_and_closes_temporary_ledger() -> None:
    source = (ROOT / "src" / "selftest.py").read_text(encoding="utf-8")
    assert "configure_event_store(db_path)" in source
    assert "close_event_store()" in source


@pytest.mark.asyncio
async def test_selftest_persists_stage_event_in_temporary_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "selftest"
    directory.mkdir()
    monkeypatch.setattr(selftest.tempfile, "mkdtemp", lambda prefix: str(directory))
    monkeypatch.setattr(selftest.shutil, "rmtree", lambda path, ignore_errors: None)

    async def record_stage(*args: object) -> bool:
        events.enter_stage(GENERATING, 1, "自检")
        return True

    async def pass_check(*args: object) -> bool:
        return True

    monkeypatch.setattr(selftest, "_check_chat", record_stage)
    monkeypatch.setattr(selftest, "_check_reflect", pass_check)
    monkeypatch.setattr(selftest, "_check_aware", pass_check)

    try:
        result = await selftest.run_selftest(Config())
    finally:
        close_db()

    with sqlite3.connect(directory / "memory.db") as connection:
        stages = connection.execute(
            "SELECT stage FROM pipeline_events WHERE kind = 'stage' ORDER BY seq",
        ).fetchall()
    assert result == 0
    assert stages == [("generating",)]


@pytest.mark.asyncio
async def test_snapshot_writes_internal_request_without_provider_request(tmp_path: Path) -> None:
    snapshot.configure(tmp_path)
    router = ModelRouter("chat", [])

    with pytest.raises(LlmError, match="没有可用模型"):
        async for _ in router.stream([{"role": "user", "content": "你好"}]):
            pass

    path = snapshot.dump("chat", "RuntimeError", "候选为空")

    assert path is not None
    payload = loads(path.read_text(encoding="utf-8"))
    assert payload["internal_request"]["task"] == "chat"
    assert payload["provider_request"] is None


def test_historical_unknown_stage_remains_replayable() -> None:
    connection = event_store._connection
    assert connection is not None
    connection.execute(
        """
        INSERT INTO pipeline_events (at, stream_id, turn_id, stage, kind, payload)
        VALUES (1, 3, 7, 'retired_stage', 'stage', '{}')
        """,
    )
    connection.commit()

    app = FastAPI()
    app.include_router(http_router)
    with TestClient(app) as client:
        response = client.get(
            "/events?kind=stage",
            headers={"Authorization": "Bearer test-token-fixture"},
        )

    assert response.status_code == 200
    entry = response.json()["events"][0]
    assert label_for("retired_stage") == "retired_stage"
    assert entry["stage"] == "retired_stage"
    assert entry["stageLabel"] == "retired_stage"
