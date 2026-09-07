"""验证 QQ 分享卡片(json / share 消息段)渲染出标题、摘要与跳转地址。

样本是构造的：卡片顶层 ``app`` / ``prompt`` 与 ``meta.detail_1`` / ``meta.news`` /
``meta.music`` 的字段路径取自 QQ 客户端实测的 ARK 结构文档，真机样本由验收方复验。
"""

from __future__ import annotations

from typing import Any, Dict, List

import json

import pytest

from src.platforms.onebot11 import cards
from src.platforms.onebot11.segments import segment_to_text


def _json_segment(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {'type': 'json', 'data': {'data': json.dumps(payload, ensure_ascii=False)}}


def _render_json(payload: Dict[str, Any]) -> str:
    return segment_to_text(_json_segment(payload))


class _LogRecorder:
    """记录 info / warning 调用的日志桩，替代真实 logger 以断言字段。"""

    def __init__(self) -> None:
        self.info_calls: List[Dict[str, Any]] = []
        self.warning_calls: List[Dict[str, Any]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.info_calls.append({'event': event, **fields})

    def warning(self, event: str, **fields: Any) -> None:
        self.warning_calls.append({'event': event, **fields})


def test_小程序卡片渲染出标题摘要与地址() -> None:
    payload = {
        'app': 'com.tencent.miniapp_01',
        'prompt': '[QQ小程序]哔哩哔哩',
        'meta': {
            'detail_1': {
                'appid': '1109937557',
                'title': '某视频标题',
                'desc': 'UP主：某某',
                'qqdocurl': 'https://b23.tv/abc1234',
            },
        },
        'ver': '1.0.0.1',
    }

    assert _render_json(payload) == '[QQ小程序哔哩哔哩：某视频标题——UP主：某某｜https://b23.tv/abc1234]'


def test_图文卡片命中news的jumpUrl() -> None:
    payload = {
        'app': 'com.tencent.tuwen.lua',
        'prompt': '[图文]',
        'meta': {
            'news': {
                'title': '某文章标题',
                'desc': '摘要文字',
                'jumpUrl': 'https://example.com/article/1',
            },
        },
    }

    assert _render_json(payload) == '[图文：某文章标题——摘要文字｜https://example.com/article/1]'


def test_音乐卡片命中musicUrl且mqqapi不算地址() -> None:
    payload = {
        'app': 'com.tencent.structmsg',
        'prompt': '[分享]某歌曲',
        'meta': {
            'music': {
                'title': '某歌曲',
                'desc': '某歌手',
                'jumpUrl': 'mqqapi://card/show_pslcard?src_type=internal',
                'musicUrl': 'https://music.example.com/song/1',
            },
        },
    }

    assert _render_json(payload) == '[分享某歌曲：某歌曲——某歌手｜https://music.example.com/song/1]'


def test_嵌套JSON字符串里的地址仍能取到() -> None:
    inner = json.dumps({
        'detail_1': {
            'title': '嵌套标题',
            'desc': '嵌套摘要',
            'qqdocurl': 'https://b23.tv/nested9',
        },
    }, ensure_ascii=False)
    payload = {
        'app': 'com.tencent.miniapp_01',
        'prompt': '[QQ小程序]测试',
        'meta': inner,
    }

    assert _render_json(payload) == '[QQ小程序测试：嵌套标题——嵌套摘要｜https://b23.tv/nested9]'


def test_无地址站内卡片渲染标签与标题且不含地址() -> None:
    payload = {
        'app': 'com.tencent.announcement',
        'prompt': '[群公告]',
        'meta': {'announce': {'title': '本周活动安排'}},
    }

    text = _render_json(payload)

    assert text == '[群公告：本周活动安排]'
    assert 'http' not in text


def test_非合法JSON与非对象与缺失都退回原占位符() -> None:
    assert segment_to_text({'type': 'json', 'data': {'data': '{不是合法JSON'}}) == '[JSON 消息]'
    assert segment_to_text({'type': 'json', 'data': {'data': '[1, 2, 3]'}}) == '[JSON 消息]'
    assert segment_to_text({'type': 'json', 'data': {}}) == '[JSON 消息]'


def test_只有mqqapi地址的卡片视为无地址() -> None:
    payload = {
        'app': 'com.tencent.game',
        'prompt': '[游戏]',
        'meta': {'detail_1': {'title': '某游戏', 'url': 'mqqapi://openapi/jump'}},
    }

    assert _render_json(payload) == '[游戏：某游戏]'


def test_超长摘要按上限截断() -> None:
    desc = '摘' * (cards._MAX_DESC_LENGTH + 40)
    payload = {
        'app': 'com.tencent.tuwen.lua',
        'prompt': '[图文]',
        'meta': {'news': {'title': '标题', 'desc': desc, 'jumpUrl': 'https://example.com/a'}},
    }

    text = _render_json(payload)

    assert text == f'[图文：标题——{desc[:cards._MAX_DESC_LENGTH]}｜https://example.com/a]'
    assert len(text) < cards._MAX_DESC_LENGTH + 40


def test_环状结构不死循环不抛异常() -> None:
    cyclic: Dict[str, Any] = {'title': '环状标题'}
    cyclic['self'] = cyclic

    text = segment_to_text({'type': 'share', 'data': {'title': cyclic, 'content': '', 'url': ''}})

    assert '环状标题' in text


def test_超深结构有界停靠() -> None:
    deep: Dict[str, Any] = {}
    node = deep
    for _ in range(20):
        node['next'] = {}
        node = node['next']
    node['url'] = 'https://example.com/too/deep'

    text = segment_to_text({'type': 'share', 'data': {'title': '深', 'content': '', 'url': deep}})

    assert 'https://example.com/too/deep' not in text


def test_share段走同一渲染路径() -> None:
    segment = {
        'type': 'share',
        'data': {'url': 'https://b23.tv/share1', 'title': '分享标题', 'content': '分享摘要'},
    }

    assert segment_to_text(segment) == '[分享：分享标题——分享摘要｜https://b23.tv/share1]'


def test_share段什么都没取到时退回原占位符() -> None:
    assert segment_to_text({'type': 'share', 'data': {}}) == '[分享]'


def test_找不到地址时记一条只含app名的info日志(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(cards, 'logger', recorder)

    _render_json({'app': 'com.tencent.announcement', 'meta': {'announce': {'title': '公告'}}})

    assert len(recorder.info_calls) == 1
    assert recorder.info_calls[0] == {'event': '分享卡片未找到跳转地址', 'app': 'com.tencent.announcement'}


def test_取到地址时不记日志(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(cards, 'logger', recorder)

    _render_json({'app': 'com.tencent.tuwen.lua', 'meta': {'news': {'jumpUrl': 'https://example.com/a'}}})

    assert recorder.info_calls == []


def test_xml段保持原样() -> None:
    assert segment_to_text({'type': 'xml', 'data': {'data': '<msg/>'}}) == '[XML 消息]'
