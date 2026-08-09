"""日志落盘：JSONL 文件，按体积换文件，按数量和天数清理。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, MutableMapping

import json
import threading
import time

_FILE_PREFIX = 'app_'
_FILE_SUFFIX = '.log.jsonl'
# 这几个键有专属位置，不再重复进 fields
_RESERVED_KEYS = frozenset({'timestamp', 'level', 'logger', 'event', 'exception'})


def _file_name(moment: datetime) -> str:
    return f'{_FILE_PREFIX}{moment.strftime("%Y%m%d_%H%M%S")}{_FILE_SUFFIX}'


class JsonlFileSink:
    """把日志行写成 JSONL，并维护文件数量与保留天数。"""

    def __init__(
        self,
        directory: Path,
        max_bytes: int,
        max_files: int,
        cleanup_days: int,
    ) -> None:
        self._directory = directory
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._cleanup_days = cleanup_days
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._size = 0
        # 进程内只增不减：重置会让新文件重名成旧序号，排序就不再等于时间序
        self._serial = 0

    def write(self, payload: Dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        encoded = line.encode('utf-8') + b'\n'
        with self._lock:
            self._ensure_open(len(encoded))
            assert self._path is not None
            with self._path.open('ab') as handle:
                handle.write(encoded)
            self._size += len(encoded)

    def current_path(self) -> Path | None:
        """当前正在写的文件，还没写过则为 None。"""
        return self._path

    def _ensure_open(self, incoming: int) -> None:
        """写满就换新文件，并清理旧文件。"""
        if self._path is not None and self._size + incoming <= self._max_bytes:
            return
        self._directory.mkdir(parents=True, exist_ok=True)
        self._serial += 1
        stem = _file_name(datetime.now()).removesuffix(_FILE_SUFFIX)
        self._path = self._directory / f'{stem}_{self._serial:04d}{_FILE_SUFFIX}'
        self._path.touch()
        self._size = 0
        self._prune()

    def _prune(self) -> None:
        """按天数和数量删旧文件，当前正在写的那份除外。"""
        files = self._existing_files()
        if self._cleanup_days > 0:
            deadline = time.time() - self._cleanup_days * 86_400
            for path in list(files):
                if path != self._path and path.stat().st_mtime < deadline:
                    path.unlink(missing_ok=True)
                    files.remove(path)
        while len(files) > self._max_files:
            oldest = files.pop(0)
            if oldest == self._path:
                continue
            oldest.unlink(missing_ok=True)

    def _existing_files(self) -> List[Path]:
        """目录里的日志文件，按修改时间升序；同刻的按文件名定序。"""
        if not self._directory.exists():
            return []
        files = list(self._directory.glob(f'{_FILE_PREFIX}*{_FILE_SUFFIX}'))
        return sorted(files, key=lambda path: (path.stat().st_mtime, path.name))


def render_json_line(event_dict: MutableMapping[str, Any]) -> Dict[str, Any]:
    """把 structlog 事件字典整成落盘形状；kwargs 收进 fields，便于 jq 过滤。"""
    payload: Dict[str, Any] = {
        'timestamp': str(event_dict.get('timestamp', '')),
        'level': str(event_dict.get('level', 'info')),
        'logger': str(event_dict.get('logger', '')),
        'event': str(event_dict.get('event', '')),
    }
    fields = {
        key: value for key, value in event_dict.items()
        if key not in _RESERVED_KEYS
    }
    if fields:
        payload['fields'] = json.loads(json.dumps(fields, ensure_ascii=False, default=str))
    exception = event_dict.get('exception')
    if exception:
        payload['exception'] = str(exception)
    return payload
