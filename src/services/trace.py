"""
运行时调试追踪：用户输入 → LLM 决策 → LLM 返回，供 Observability 窗口和
日志文件调试用。

设计取舍：
  · 不走 WS 推送——client.ts 现在的 WS→IPC 转发只送到桌宠窗口
    （windowSink(petWindow)），Observability 是独立窗口收不到；新增多窗口
    广播的代价比让前端轮询 GET /debug/trace?since=<seq> 大得多。
  · emit() 全程同步，不需要事件循环，可以从任何地方直接调用。
  · 正文原样记录：用户输入、整个 messages（含 system prompt 与召回的记忆）、
    模型输出都不做处理。单机部署，trace 的全部价值就是能照原样看到发出去的
    prompt；体积只由 trace_max_bytes 的轮转兜住。
  · 落盘是"永远写"，不受 advanced.log_level 控制——调试时不想为了看一眼
    trace.jsonl 就把整个应用的日志级别调到 DEBUG。同时也走一遍
    logger.debug()，级别调到 DEBUG 后终端也能看到。
"""

from __future__ import annotations

import json
from collections import deque
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict

from src.common.clock import now as current_time
from src.common.logger import get_logger

logger = get_logger(__name__)


# 当前这一轮消息的来源，由 ChatService.send() 在入口绑定一次，emit() 自动带上。
# 用 ContextVar 是因为 llm_request / llm_final 这些 kind 在 LLM 层发出，
# 不该为了记一句「这是谁说的」把 stream 和 person 一路透传下去。
# 每轮跑在自己的 task 里，ContextVar 天然按轮隔离。
_origin: ContextVar[Dict[str, Any]] = ContextVar('trace_origin', default={})


def bind_origin(
    stream_id: int,
    platform: str,
    person_id: int,
    person_kind: str,
) -> None:
    """绑定本轮的消息来源，之后这一轮的每条 trace 都会带上。"""
    _origin.set({
        'streamId': stream_id,
        'platform': platform,
        'personId': person_id,
        'personKind': person_kind,
    })


class TraceBuffer:
    def __init__(self, maxlen: int = 1000) -> None:
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        self._log_path: Path | None = None
        self._max_bytes = 8 * 1024 * 1024

    def configure(self, log_path: Path | None, max_bytes: int = 8 * 1024 * 1024) -> None:
        """指定落盘位置；传 None 关闭落盘（环形缓冲和 logger.debug 仍然生效）。"""
        self._log_path = log_path
        self._max_bytes = max_bytes
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, **fields: Any) -> dict:
        self._seq += 1
        entry = {"seq": self._seq, "at": current_time(), "kind": kind, **_origin.get(), **fields}
        self._buf.append(entry)
        logger.debug("trace", **entry)
        if self._log_path:
            try:
                self._rotate_if_needed()
                with self._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            except Exception:
                pass   # 落盘失败（磁盘满/权限）不该把对话链路带崩
        return entry

    def _rotate_if_needed(self) -> None:
        """超过上限就轮转成 .1（只留一代），避免无限增长。"""
        path = self._log_path
        if path is None or self._max_bytes <= 0 or not path.exists():
            return
        if path.stat().st_size < self._max_bytes:
            return
        backup = path.with_suffix(path.suffix + ".1")
        try:
            backup.unlink(missing_ok=True)
            path.rename(backup)
        except Exception:
            pass

    def since(self, seq: int) -> list[dict]:
        """seq 之后的全部条目，供前端增量轮询。"""
        return [e for e in self._buf if e["seq"] > seq]

    def clear(self) -> None:
        self._buf.clear()
        self._seq = 0
        self._log_path = None


# 进程级单例，和 services/lifecycle.py 的 lifecycle 同一种风格
trace = TraceBuffer()
