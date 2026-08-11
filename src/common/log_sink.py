"""将结构化日志写入 JSONL 文件并执行滚动清理。

`JsonlFileSink` 按单文件字节数切换输出文件，再按保留天数和最大文件数删除旧文件；
`render_json_line` 将 structlog 事件字典转换为稳定的落盘结构。
"""

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
    """根据时间生成日志文件的基础名称。

    :param moment: 用于文件名的本地时间。
    :return: 带固定前缀、秒级时间和 `.log.jsonl` 后缀的文件名。
    :side_effects: 不创建文件或访问文件系统。
    """
    return f'{_FILE_PREFIX}{moment.strftime("%Y%m%d_%H%M%S")}{_FILE_SUFFIX}'


class JsonlFileSink:
    """把日志行写成 JSONL，并维护文件数量与保留天数。

    写入操作由线程锁保护，单个实例适合被多个日志处理线程共享；文件滚动和清理
    只在需要创建新文件时触发。
    """

    def __init__(
        self,
        directory: Path,
        max_bytes: int,
        max_files: int,
        cleanup_days: int,
    ) -> None:
        """创建尚未打开输出文件的日志 sink。

        :param directory: 日志文件目录。
        :param max_bytes: 单文件最大字节数；写入超出后滚动。
        :param max_files: 目录最多保留的日志文件数。
        :param cleanup_days: 按修改时间清理的保留天数；小于等于 0 时禁用按天清理。
        :side_effects: 只保存配置并初始化锁，不创建目录或文件。
        """
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
        """将一个事件字典序列化为 UTF-8 JSONL 并追加到当前文件。

        :param payload: 可 JSON 序列化的结构化日志字典。
        :return: 无返回值。
        :raises TypeError: payload 中存在无法序列化的值时抛出。
        :raises OSError: 目录、滚动文件或写入操作失败时抛出。
        :side_effects: 创建日志目录/文件、追加一行内容，并可能删除旧日志文件。
        :performance: 每次写入都会执行一次 JSON 序列化和文件追加；并发写入按锁串行化。
        """
        line = json.dumps(payload, ensure_ascii=False)
        encoded = line.encode('utf-8') + b'\n'
        with self._lock:
            self._ensure_open(len(encoded))
            assert self._path is not None
            with self._path.open('ab') as handle:
                handle.write(encoded)
            self._size += len(encoded)

    def current_path(self) -> Path | None:
        """返回当前日志文件路径。

        Returns:
            当前已打开或已创建的日志文件路径；尚未写入任何事件时返回 ``None``。

        Side Effects:
            仅读取内存中的路径引用，不访问文件系统。
        """
        return self._path

    def _ensure_open(self, incoming: int) -> None:
        """确保当前文件能够容纳即将写入的字节，不足时创建新文件并清理旧文件。

        Args:
            incoming: 下一条 JSONL 记录的 UTF-8 字节数，必须为非负整数。

        Raises:
            OSError: 日志目录创建、文件创建、滚动或清理失败。

        Side Effects:
            必要时创建日志目录和新文件，更新当前路径及大小，并调用 ``_prune``。
        """
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
        """按保留天数和最大文件数清理旧日志，始终保留当前写入文件。

        Raises:
            OSError: 读取文件状态或删除旧日志失败。

        Side Effects:
            删除超过日期期限或数量上限的历史日志文件；不删除当前文件。
        """
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
        """枚举当前目录中的日志文件并按修改时间稳定排序。

        Returns:
            匹配固定前缀和后缀的日志路径列表，按修改时间升序、文件名作为同刻次序；
            目录不存在时返回空列表。

        Raises:
            OSError: 目录枚举或文件状态读取失败。
        """
        if not self._directory.exists():
            return []
        files = list(self._directory.glob(f'{_FILE_PREFIX}*{_FILE_SUFFIX}'))
        return sorted(files, key=lambda path: (path.stat().st_mtime, path.name))


def render_json_line(event_dict: MutableMapping[str, Any]) -> Dict[str, Any]:
    """将结构化日志事件规范化为稳定的 JSONL 记录结构。

    Args:
        event_dict: structlog 事件映射；保留时间、级别、logger、事件和异常字段，
            其他键归入 ``fields``。

    Returns:
        可直接 JSON 序列化的普通字典；不可序列化的 fields 值使用其字符串表示。

    Raises:
        TypeError: 事件映射不支持键访问或字段转换时抛出。

    Side Effects:
        仅读取事件映射，不修改输入对象；对 fields 执行一次 JSON 往返以统一类型。
    """
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
