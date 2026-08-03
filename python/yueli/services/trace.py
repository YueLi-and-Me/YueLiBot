"""
运行时调试追踪：用户输入 → LLM 决策 → LLM 返回，供 Observability 窗口和
日志文件调试用。

设计取舍：
  · 不走 WS 推送——client.ts 现在的 WS→IPC 转发只送到桌宠窗口
    （windowSink(petWindow)），Observability 是独立窗口收不到；新增多窗口
    广播的代价比让前端轮询 GET /debug/trace?since=<seq> 大得多。
  · emit() 全程同步，不需要事件循环，可以从任何地方直接调用。
  · 落盘是"永远写"，不受 advanced.log_level 控制——调试时不想为了看一眼
    trace.jsonl 就把整个应用的日志级别调到 DEBUG。同时也走一遍
    logger.debug()，级别调到 DEBUG 后终端也能看到。
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from yueli.common.clock import now as current_time
from yueli.common.logger import get_logger

logger = get_logger(__name__)


class TraceBuffer:
    def __init__(self, maxlen: int = 1000) -> None:
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        self._log_path: Path | None = None

    def configure(self, log_path: Path | None) -> None:
        """指定落盘位置；传 None 关闭落盘（环形缓冲和 logger.debug 仍然生效）。"""
        self._log_path = log_path
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, **fields: Any) -> dict:
        self._seq += 1
        entry = {"seq": self._seq, "at": current_time(), "kind": kind, **fields}
        self._buf.append(entry)
        logger.debug("trace", **entry)
        if self._log_path:
            try:
                with self._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            except Exception:
                pass   # 落盘失败（磁盘满/权限）不该把对话链路带崩
        return entry

    def since(self, seq: int) -> list[dict]:
        """seq 之后的全部条目，供前端增量轮询。"""
        return [e for e in self._buf if e["seq"] > seq]

    def clear(self) -> None:
        self._buf.clear()
        self._seq = 0
        self._log_path = None


# 进程级单例，和 services/lifecycle.py 的 lifecycle 同一种风格
trace = TraceBuffer()
