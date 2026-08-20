"""检索模型路由层落盘的分阶段调用记录。

记录由 ``src.core.llm_models.snapshot.dump_exchange`` 在每次模型调用收尾时写入
``<数据目录>/logs/prompt/<任务>/<时间戳>_s<streamId>.json``；本模块只做读取侧：
把目录扫描成可分页的摘要列表，并按任务与文件名读取单份完整内容。

对外暴露 ``list_tasks`` / ``list_records`` / ``read_record`` 三个函数，被
``src.core.api.http`` 的只读观察路由调用。记录目录本身由 snapshot 模块持有，
本模块不关心它配置在哪，也不负责写入与轮转。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import json
import re

from src.core.llm_models.snapshot import exchange_directory


# 任务目录名与记录文件名的合法字符集。落盘侧已经把任务名里的 '.' 换成 '-'，
# 这里再收一次是因为参数来自 HTTP：只放行不含分隔符与 '..' 的名字，杜绝用
# 路径穿越读到记录目录之外的文件。
_SAFE_TASK = re.compile(r'^[A-Za-z0-9_-]{1,120}$')
_SAFE_NAME = re.compile(r'^[A-Za-z0-9_-]{1,120}\.json$')


class RecordsDisabled(RuntimeError):
    """未启用分阶段调用记录。

    与「启用但还没有记录」区分开：前者是配置问题，面板应提示去开开关；后者只是
    还没跑过模型调用，等一会儿就有。两种情况回同一个空列表会让人白等。
    """


def list_tasks() -> List[str]:
    """列出当前已有记录的任务名。

    :return: 任务目录名列表，按字典序排列；目录尚未创建时为空列表。
    :raises RecordsDisabled: 未启用调用记录。
    副作用：只读目录项。
    """
    root = _root()
    if not root.exists():
        return []
    return sorted(item.name for item in root.iterdir() if item.is_dir())


def list_records(task: str | None, limit: int) -> List[Dict[str, Any]]:
    """按任务列出调用记录摘要，最近的在前。

    摘要逐份解析 JSON 取字段，不缓存：记录文件数受每任务保留上限约束，面板一次
    最多取几十份，直接读比维护一份会与轮转脱节的索引更可靠。

    :param task: 只看某个任务；``None`` 表示合并全部任务后按时间排序。
    :param limit: 最多返回的摘要数，必须为正。
    :return: 摘要字典列表，含任务、文件名、时间、会话与回合、模型、耗时与是否失败。
    :raises RecordsDisabled: 未启用调用记录。
    :raises ValueError: ``task`` 含非法字符，或 ``limit`` 非正。
    副作用：只读文件内容。
    """
    if limit <= 0:
        raise ValueError('limit 必须为正整数')
    root = _root()
    if task is not None:
        _require_safe_task(task)
        directories = [root / task]
    else:
        directories = [item for item in root.iterdir() if item.is_dir()] if root.exists() else []

    files: List[Path] = []
    for directory in directories:
        if directory.exists():
            files.extend(directory.glob('*.json'))
    # 按修改时间倒序取最近的若干份；文件名里的时间戳与 mtime 同源，用 mtime
    # 可以避免解析文件名格式，跨任务合并时也是同一把尺子。
    files.sort(key=lambda item: (item.stat().st_mtime, item.name), reverse=True)

    summaries: List[Dict[str, Any]] = []
    for path in files[:limit]:
        summary = _summarize(path)
        if summary is not None:
            summaries.append(summary)
    return summaries


def read_record(task: str, name: str) -> Dict[str, Any]:
    """读取单份调用记录的完整内容。

    :param task: 任务目录名。
    :param name: 记录文件名，必须以 ``.json`` 结尾。
    :return: 记录的完整 JSON 内容。
    :raises RecordsDisabled: 未启用调用记录。
    :raises ValueError: 任务名或文件名含非法字符。
    :raises FileNotFoundError: 记录不存在。
    副作用：只读单个文件。
    """
    _require_safe_task(task)
    if not _SAFE_NAME.match(name):
        raise ValueError(f'非法的记录文件名：{name}')
    path = _root() / task / name
    if not path.is_file():
        raise FileNotFoundError(f'记录不存在：{task}/{name}')
    return json.loads(path.read_text(encoding='utf-8'))


def _root() -> Path:
    """返回记录根目录。

    :raises RecordsDisabled: 未配置记录目录。
    """
    root = exchange_directory()
    if root is None:
        raise RecordsDisabled('未启用分阶段调用记录')
    return root


def _require_safe_task(task: str) -> None:
    """校验任务目录名，拒绝路径穿越。"""
    if not _SAFE_TASK.match(task):
        raise ValueError(f'非法的任务名：{task}')


def _summarize(path: Path) -> Dict[str, Any] | None:
    """把一份记录解析成列表所需的摘要。

    :param path: 记录文件路径。
    :return: 摘要字典；文件正在写入或内容损坏时返回 ``None``，跳过该份而不是
        让整个列表失败——一份坏文件不该挡住其余记录的查看。
    """
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get('model') or {}
    timing = payload.get('timing') or {}
    response = payload.get('response') or {}
    error = payload.get('error')
    return {
        'task': payload.get('task', path.parent.name),
        'name': path.name,
        'at': payload.get('at'),
        'stage': payload.get('stage'),
        'streamId': payload.get('streamId'),
        'turnId': payload.get('turnId'),
        'model': model.get('name'),
        'provider': model.get('provider'),
        'firstTokenMs': timing.get('firstTokenMs'),
        'totalMs': timing.get('totalMs'),
        'chunks': response.get('chunks'),
        'textLength': len(response.get('text') or ''),
        'errorType': (error or {}).get('type') if error else None,
    }
