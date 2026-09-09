"""四个事实读取入口的调用纪律守卫（★S-6）。

用 AST 扫描 src/ 下全部调用点：漏传 ``stream_kind`` 的裸调用会让可见性过滤
形同虚设；旁路值 ``'all'`` 只允许出现在事实抽取的去重清单那一个调用点。
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / 'src'
_ENTRY_POINTS = {
    'recall_facts',
    'recall_facts_in_scope',
    'recall_facts_across_persons',
    'top_facts',
}
_ALL_BYPASS_FILE = 'fact_extract.py'


def _iter_entry_calls():
    """产出 (文件相对路径, 节点) 的读取入口调用点。"""
    for path in _SRC_ROOT.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _ENTRY_POINTS
            ):
                yield path.relative_to(_SRC_ROOT), node


def test_no_call_site_omits_stream_kind() -> None:
    """★S-6：全项目没有裸调用四个入口的地方。"""
    missing = []
    for rel, node in _iter_entry_calls():
        has = any(kw.arg == 'stream_kind' for kw in node.keywords)
        if not has:
            missing.append(f'{rel}:{node.lineno}')
    assert not missing, f'这些调用点漏传 stream_kind：{missing}'


def test_all_bypass_only_in_fact_extract_dedup() -> None:
    """旁路值 'all' 只允许出现在 fact_extract 的去重清单调用点。"""
    offenders = []
    for rel, node in _iter_entry_calls():
        for kw in node.keywords:
            if kw.arg != 'stream_kind':
                continue
            value = kw.value
            is_all = (
                isinstance(value, ast.Constant) and value.value == 'all'
            )
            if is_all and rel.parts[-1] != _ALL_BYPASS_FILE:
                offenders.append(f'{rel}:{node.lineno}')
    assert not offenders, f"'all' 只允许在 {_ALL_BYPASS_FILE} 使用，越界：{offenders}"
