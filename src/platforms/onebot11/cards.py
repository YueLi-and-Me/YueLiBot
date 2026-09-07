"""把 QQ 分享类消息段(json / share)的卡片结构渲染成带标题、摘要与跳转地址的占位文本。

QQ 客户端会把粘贴的视频、文章、音乐链接自动转成结构化卡片；这些卡片的标题、摘要、
来源与跳转地址本来就随消息段一起到达，不渲染出来，主体看到的就只有 ``[JSON 消息]``
四个字。本模块在解析出的卡片结构上做优先键有界递归扫描，把已有信息渲染进正文；
全程纯函数，不读文件、不出网。

刻意不做的两件事：

- 不按卡片顶层 ``app`` 字段(``com.tencent.miniapp_01`` 之类)列分支表。QQ 的卡片类型
  只增不减，分支表漏一个就静默退回 ``[JSON 消息]``，而漏掉的类型与「这类卡片本来就
  没链接」在现场长得一模一样，无法归因；那份清单永远追不齐，也没人知道追没追齐。
- ``xml`` 段不在本模块处理，仍渲染为 ``[XML 消息]``。这是刻意的边界而不是遗漏：
  该形态样本罕见，而解析 XML 要额外考虑外部实体，为一个低频形态引入那份风险不划算。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

import json

from src.core.logging.logger import get_logger

logger = get_logger(__name__)

# 地址键按优先级排列：小程序卡片的跳转地址稳定落在 meta.detail_1.qqdocurl，
# 图文卡片在 meta.news.jumpUrl，音乐卡片在 meta.music.musicUrl；url 是 share 段
# 与部分扁平卡片的直接字段。只认这个有序清单，不按键名猜——卡片结构里还散布着
# 大量图标、封面图地址(常叫 icon / cover / picture)，放开匹配会把封面图当成跳转地址。
_URL_KEYS = ('qqdocurl', 'jumpUrl', 'jump_url', 'musicUrl', 'url', 'source_url')
_TITLE_KEYS = ('title',)
_DESC_KEYS = ('desc', 'summary')
# 卡片自述，形如「[QQ小程序]哔哩哔哩」「[图文]」，用作占位符首段的标签。
_PROMPT_KEYS = ('prompt', 'tag')

# 递归深度上限。卡片是协议端投递的用户可控数据，不设上限时一个构造出来的深层
# 结构能把栈耗光；6 层足以覆盖「JSON 字符串套 JSON 字符串」的 ARK 形态。
_MAX_SCAN_DEPTH = 6

# 摘要截断上限。卡片摘要长度不可控，不截断会让一条分享吃掉一大块提示词预算。
_MAX_DESC_LENGTH = 60

_DEFAULT_TAG = '分享'
_JSON_FALLBACK = '[JSON 消息]'
_SHARE_FALLBACK = '[分享]'


def render_card_placeholder(segment_type: str, data: Mapping[str, Any]) -> str:
    """把 ``share`` 或 ``json`` 消息段渲染成带卡片元信息的占位文本。

    渲染形态：

    - 有地址、有标题、有摘要：``[标签：标题——摘要前 60 字｜地址]``
    - 有地址、无摘要：``[标签：标题｜地址]``；有地址、无标题：``[标签：摘要｜地址]``
    - 无地址(站内卡片)：``[标签：标题]``
    - 什么都没取到：``json`` 段退回 ``[JSON 消息]``，``share`` 段退回 ``[分享]``，
      均与引入本模块前的占位逐字一致。

    标签取卡片自述键(``prompt`` / ``tag``)剥掉方括号后的短文本，取不到用「分享」。
    地址原样保留、不做清洗或缩短——它要被后续的链接读取工具原样使用。

    :param segment_type: 消息段类型，只处理 ``share`` 与 ``json``。
    :param data: 消息段的对象型 ``data`` 字段。
    :return: 占位文本；任何结构问题都退回对应段类型的原占位符，绝不抛异常。
        本函数在入站主链路上，一条畸形卡片让整条消息进不来是不可接受的。

    副作用：
        找不到跳转地址时记一条 info 日志，字段只含卡片顶层 ``app`` 名——体积与
        隐私都不允许记整段 JSON，而这条日志是后续收集真实卡片类型的唯一途径。
        不读文件、不访问网络、不修改输入。
    """
    app = ''
    fallback = _JSON_FALLBACK if segment_type == 'json' else _SHARE_FALLBACK
    try:
        if segment_type == 'share':
            # share 段的三个字段直接可取，归一化成扫描器认识的键后走同一渲染路径，
            # 不为它另写一份格式。
            payload: Mapping[str, Any] = {
                'title': data.get('title'),
                'desc': data.get('content'),
                'url': data.get('url'),
            }
        else:
            raw = data.get('data')
            if not isinstance(raw, str):
                return fallback
            parsed = json.loads(raw)
            if not isinstance(parsed, Mapping):
                return fallback
            payload = parsed
            app_value = payload.get('app')
            app = app_value if isinstance(app_value, str) else ''
        card = _Card(
            url=_scan(payload, _URL_KEYS, _is_http_url),
            title=_scan(payload, _TITLE_KEYS, _is_non_empty),
            desc=_scan(payload, _DESC_KEYS, _is_non_empty),
            tag=_scan(payload, _PROMPT_KEYS, _is_non_empty),
        )
    except Exception:
        # 扫描面对的是协议端投递的任意结构，兜底一次，保入站主链路。
        logger.warning('卡片解析异常，退回原占位符', app=app)
        return fallback
    if not card.url:
        logger.info('分享卡片未找到跳转地址', app=app)
    return _render(card, fallback)


class _Card:
    """一次扫描取到的四个字段；空字符串表示没取到。"""

    __slots__ = ('url', 'title', 'desc', 'tag')

    def __init__(self, url: str, title: str, desc: str, tag: str) -> None:
        self.url = url
        self.title = title
        self.desc = desc
        self.tag = tag


def _render(card: _Card, fallback: str) -> str:
    """按有无地址、标题、摘要组合占位文本；什么都没取到时退回原占位符。"""
    if not (card.url or card.title or card.desc):
        return fallback
    tag = _strip_brackets(card.tag) or _DEFAULT_TAG
    body = card.title or _truncate_desc(card.desc)
    if card.title and card.desc:
        body = f'{card.title}——{_truncate_desc(card.desc)}'
    if card.url:
        return f'[{tag}：{body}｜{card.url}]' if body else f'[{tag}：{card.url}]'
    return f'[{tag}：{body}]' if body else fallback


def _scan(
    node: Any,
    keys: Iterable[str],
    accept: Callable[[str], bool],
    depth: int = _MAX_SCAN_DEPTH,
    seen: set[int] | None = None,
) -> str:
    """在卡片结构上按优先键有界递归扫描，返回第一个 ``accept`` 命中的字符串。

    规则：遇到 ``dict`` 先按优先键顺序下探，再遍历其余键；遇到 ``list`` 逐项下探；
    字符串值若以 ``{`` 或 ``[`` 开头，尝试再 ``json.loads`` 一层——ARK 结构里确实
    存在「JSON 字符串套 JSON 字符串」，不解这一层会漏掉地址。用 ``id()`` 集合防环，
    深度超限即停。
    """
    if depth <= 0:
        return ''
    if seen is None:
        seen = set()
    if isinstance(node, Mapping):
        if not _enter(node, seen):
            return ''
        key_list = tuple(keys)
        for key in key_list:
            if key in node:
                hit = _accept_or_descend(node[key], key_list, accept, depth - 1, seen)
                if hit:
                    return hit
        for key, value in node.items():
            if key in key_list:
                continue
            hit = _scan(value, key_list, accept, depth - 1, seen)
            if hit:
                return hit
        return ''
    if isinstance(node, (list, tuple)):
        if not _enter(node, seen):
            return ''
        for item in node:
            hit = _scan(item, keys, accept, depth - 1, seen)
            if hit:
                return hit
        return ''
    if isinstance(node, str):
        return _descend_json_string(node, keys, accept, depth - 1, seen)
    return ''


def _accept_or_descend(
    value: Any,
    keys: Iterable[str],
    accept: Callable[[str], bool],
    depth: int,
    seen: set[int],
) -> str:
    """优先键上的值：命中判定优先；是嵌套 JSON 字符串或结构时继续下探。"""
    if isinstance(value, str):
        normalized = _squash(value)
        if value.lstrip()[:1] in ('{', '['):
            hit = _descend_json_string(value, keys, accept, depth, seen)
            if hit:
                return hit
        return normalized if accept(normalized) else ''
    return _scan(value, keys, accept, depth, seen)


def _descend_json_string(
    value: str,
    keys: Iterable[str],
    accept: Callable[[str], bool],
    depth: int,
    seen: set[int],
) -> str:
    """对形如 JSON 的字符串再解一层；解不动就按普通字符串处理，不算命中。"""
    if depth <= 0 or value.lstrip()[:1] not in ('{', '['):
        return ''
    try:
        parsed = json.loads(value)
    except ValueError:
        return ''
    if isinstance(parsed, (Mapping, list, tuple)):
        return _scan(parsed, keys, accept, depth, seen)
    return ''


def _enter(node: Any, seen: set[int]) -> bool:
    """按 ``id()`` 登记容器身份，已在集合中说明遇到环，不再下探。"""
    ident = id(node)
    if ident in seen:
        return False
    seen.add(ident)
    return True


def _is_http_url(value: str) -> bool:
    """只认 http(s) 地址；卡片里有大量 mqqapi:// 这类唤起协议，那些不是可读链接。"""
    return value.startswith('http://') or value.startswith('https://')


def _is_non_empty(value: str) -> bool:
    return bool(value)


def _squash(value: str) -> str:
    """把值里的连续空白(含换行)压成单个空格，占位符必须保持单行。"""
    return ' '.join(value.split())


def _strip_brackets(value: str) -> str:
    """剥掉自述文本里的半角方括号。

    占位符本身以 ``[...]`` 为界，自述值形如「[QQ小程序]哔哩哔哩」，不剥括号会在
    占位符里出现嵌套方括号，合并转发按 ``\\[[^\\[\\]]*\\]`` 匹配占位符时会被截断。
    """
    return value.replace('[', '').replace(']', '').strip()


def _truncate_desc(desc: str) -> str:
    """摘要按模块常量截断：卡片摘要长度不可控，不截会吃掉大块提示词预算。"""
    return desc[:_MAX_DESC_LENGTH]
