"""识别旧式「聊天记录」XML 卡片并渲染其自带预览，其余 XML 形态退回原占位符。

旧式聊天记录卡片（根元素 ``msg``，且 ``serviceID="35"`` 或 ``action="viewMultiMsg"``）
只携带标题与预览行，没有可再取一层的资源编号；不渲染出来，主体看到的就只有
``[XML 消息]``，只能据此猜测卡片内容。本模块在消息段渲染阶段把卡片自带的标题与
预览行读进正文，并明确标注卡片只带预览、打不开；其余 XML 形态一律退回
``[XML 消息]``，记一条 info 日志积累形态样本，不为没有样本的形态写解析。

安全边界：卡片是协议端投递的用户可控数据。解析前的两道前置检查把外部实体与
实体膨胀挡在解析器之外——声明了 ``DOCTYPE`` 或 ``ENTITY``（不区分大小写）的输入、
超过 16 KB 的输入都直接退回原占位符，不进入解析器；解析本身用标准库
``xml.etree.ElementTree``，不会解析外部实体，内部实体随 DOCTYPE 前置检查整体拒绝。
解析失败同样退回原占位符。全程纯函数，不读文件、不出网；渲染文本与日志字段都
不携带卡片全文。

渲染形态（预览行与条数段按内容取舍）::

    [聊天记录卡片：群聊的聊天记录｜QQ用户：（聊天记录）｜QQ用户：（图片）｜共 2 条，卡片只带预览，打不开]

卡片文本里的半角方括号一律换成全角圆括号、冒号连同紧贴空白换成全角冒号、
连续空白压成一个：正文按出现顺序替换 ``[图片]`` 占位、合并转发按整段方括号
判断占位，渲染文本内残留半角方括号会同时破坏这两处。
"""

from __future__ import annotations

from typing import Any, Mapping
import re

import xml.etree.ElementTree as ElementTree

from src.core.logging.logger import get_logger

logger = get_logger(__name__)

# 非聊天记录形态与被前置检查拒绝的输入共用这一个占位符，与 segments.py
# 处理 xml 段前的原占位逐字一致。
XML_CARD_PLACEHOLDER = '[XML 消息]'

# 旧式聊天记录卡片的判定条件：根元素 msg，且两个属性命中其一。
_CHAT_RECORD_ROOT_TAG = 'msg'
_CHAT_RECORD_SERVICE_ID = '35'
_CHAT_RECORD_ACTION = 'viewMultiMsg'

# 解析前置检查。声明了 DOCTYPE 或 ENTITY 的输入可能携带外部实体或实体膨胀，
# 不区分大小写整体拒绝；16 KB 上限挡住不声明实体也可能拖垮解析的巨型文档。
_REFUSED_DECLARATIONS = ('<!doctype', '<!entity')
_MAX_XML_BYTES = 16 * 1024

# 渲染边界：预览行最多 4 行；单行超过 30 字截断，超出部分直接丢弃。
_MAX_PREVIEW_ROWS = 4
_MAX_ROW_CHARS = 30
_ELLIPSIS = '…'

# 渲染文本的首标记同时供转发单行预览识别这类片段；完整渲染与短标记的
# 分工由 forward.py 维护。
CHAT_RECORD_CARD_PREFIX = '[聊天记录卡片：'
_CHAT_RECORD_CARD_SUFFIX = ']'
_CARD_PREVIEW_TAIL = '卡片只带预览，打不开'

# 冒号连同紧贴的空白一起换成全角冒号：预览行的「QQ用户: …」形态渲染后
# 不残留半角冒号，也不在冒号后留多余空格。
_COLON_PATTERN = re.compile(r'\s*:\s*')


def render_xml_card(data: Mapping[str, Any]) -> str:
    """把 ``xml`` 消息段的 ``data`` 渲染为占位文本，绝不抛异常。

    旧式聊天记录卡片渲染为完整占位文本；其余输入一律退回
    :data:`XML_CARD_PLACEHOLDER`。``tSum`` 缺失或不是正整数时省略「共 N 条，」段。

    :param data: 消息段的对象型 ``data`` 字段；只读取其中的 ``data`` 子字段，
        其余键（含协议端错填的 ``resid``）不参与识别。
    :return: 渲染文本。
    副作用：
        按失败来源记日志：超过 16 KB 与解析失败各记一条 warning，带原因；
        解析成功但不是聊天记录卡片记一条 info，字段只含 ``serviceID``、
        ``action``、``brief`` 三个属性值，不记卡片全文；聊天记录卡片携带
        ``m_resid`` 时另记一条 info，只记属性存在这一件事，不记值。
        带资源编号的卡片同样只渲染预览，不向协议端补取内容。
        不读文件、不出网、不修改输入。
    """
    raw = data.get('data')
    if not isinstance(raw, str) or not raw.strip():
        logger.warning('xml_card_parse_failed', reason='data.data 缺失或不是字符串')
        return XML_CARD_PLACEHOLDER
    lowered = raw.lower()
    if any(marker in lowered for marker in _REFUSED_DECLARATIONS):
        return XML_CARD_PLACEHOLDER
    actual_bytes = len(raw.encode('utf-8'))
    if actual_bytes > _MAX_XML_BYTES:
        logger.warning(
            'xml_card_too_large',
            reason=f'实际字节数={actual_bytes}；上限={_MAX_XML_BYTES}',
        )
        return XML_CARD_PLACEHOLDER
    try:
        root = ElementTree.fromstring(raw.encode('utf-8'))
    except Exception as exc:
        logger.warning('xml_card_parse_failed', reason=_failure_reason(exc))
        return XML_CARD_PLACEHOLDER
    service_id = _attribute(root, 'serviceID')
    action = _attribute(root, 'action')
    if root.tag != _CHAT_RECORD_ROOT_TAG or (
        service_id != _CHAT_RECORD_SERVICE_ID and action != _CHAT_RECORD_ACTION
    ):
        logger.info(
            'xml_card_unrecognized',
            serviceId=service_id,
            action=action,
            brief=_attribute(root, 'brief'),
        )
        return XML_CARD_PLACEHOLDER
    if _attribute(root, 'm_resid'):
        logger.info('chat_record_card_with_resid')
    return _render_chat_record(root)


def is_chat_record_card_text(text: str) -> bool:
    """判断一段文本片段是否为本模块渲染的聊天记录卡片占位。

    转发单行预览用它把完整渲染换成短标记；按首尾标记判定，不回头解析内容。
    渲染文本内的半角方括号已全部换成全角圆括号，首尾标记不会与正文里的
    其他方括号结构混淆。
    """
    return text.startswith(CHAT_RECORD_CARD_PREFIX) and text.endswith(_CHAT_RECORD_CARD_SUFFIX)


def _render_chat_record(root: ElementTree.Element) -> str:
    """渲染聊天记录卡片：标题、最多 4 行预览、条数段与只带预览的说明。

    第一个 ``title`` 是卡片标题，其余 ``title`` 是预览行；``summary`` 是按钮
    文字，不进渲染。空白行不保留，也不占用预览行的 4 行配额。
    """
    rows = [
        normalized
        for element in root.iter('title')
        if (normalized := _normalize_row(''.join(element.itertext())))
    ]
    title = _cap_row(rows[0]) if rows else ''
    previews = [_cap_row(row) for row in rows[1:_MAX_PREVIEW_ROWS + 1]]
    pieces = [row for row in (title, *previews) if row]
    t_sum = _positive_int(_attribute(root, 'tSum'))
    tail = f'共 {t_sum} 条，{_CARD_PREVIEW_TAIL}' if t_sum else _CARD_PREVIEW_TAIL
    pieces.append(tail)
    return f'{CHAT_RECORD_CARD_PREFIX}{"｜".join(pieces)}{_CHAT_RECORD_CARD_SUFFIX}'


def _normalize_row(raw: str) -> str:
    """对单行文本做占位安全变换：换括号、换冒号、压空白。"""
    text = raw.replace('[', '（').replace(']', '）')
    text = _COLON_PATTERN.sub('：', text)
    return ' '.join(text.split())


def _cap_row(row: str) -> str:
    """单行超过渲染上限时截断，末尾补一个省略号。"""
    if len(row) > _MAX_ROW_CHARS:
        return row[:_MAX_ROW_CHARS] + _ELLIPSIS
    return row


def _positive_int(value: str) -> int | None:
    """把属性值解析为正整数；缺失或非法时返回 ``None``。"""
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _attribute(element: ElementTree.Element, name: str) -> str:
    """读取元素属性并去除首尾空白；缺失时返回空字符串。"""
    value = element.attrib.get(name)
    return value.strip() if isinstance(value, str) else ''


def _failure_reason(exc: Exception) -> str:
    """取解析失败的可用原因；异常文本为空时退回异常类型名。"""
    return str(exc).strip() or type(exc).__name__


__all__ = [
    'CHAT_RECORD_CARD_PREFIX',
    'XML_CARD_PLACEHOLDER',
    'is_chat_record_card_text',
    'render_xml_card',
]
