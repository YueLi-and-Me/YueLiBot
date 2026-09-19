"""验证旧式「聊天记录」XML 卡片的识别、渲染与转发预览降级。

固定样本取自 2026-09-19 真机抓到的原始卡片（任务书第一节），逐字不改。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
import xml.etree.ElementTree

from structlog.testing import capture_logs

from src.core.services.media.chat_image import merge_image_descriptions
from src.platforms.onebot11.forward import forward_tree_preview, parse_forward_content
from src.platforms.onebot11.xml_cards import render_xml_card

# 真机原始样本：合并转发里第二个节点的 xml 段 data.data，原样保留。
RAW_SAMPLE_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '    <msg brief="[聊天记录]" m_fileName="MultiMsg_2A9749E3-F5D7-4EC5-A5F5-7C7BF43339E5" '
    'action="viewMultiMsg" tSum="2" flag="3" serviceID="35" m_fileSize="100"  > <item layout="1"> '
    '<title color="#000000" size="34" > 群聊的聊天记录 </title> <title color="#000000" size="26" > '
    'QQ用户: [聊天记录] </title> <title color="#000000" size="26" > QQ用户: [图片] </title> '
    '<title color="#000000" size="26" > QQ用户: [图片] </title>  <hr></hr> <summary color="#808080" '
    'size="26" > 查看转发消息  </summary> </item><source name="聊天记录"></source> </msg>'
)

# 任务书 D3 给出的逐字示例。
EXPECTED_SAMPLE_RENDER = (
    '[聊天记录卡片：群聊的聊天记录｜QQ用户：（聊天记录）｜QQ用户：（图片）'
    '｜QQ用户：（图片）｜共 2 条，卡片只带预览，打不开]'
)

XML_PLACEHOLDER = '[XML 消息]'


def _xml_segment(raw: str) -> Dict[str, Any]:
    """构造 OneBot xml 消息段。"""
    return {'type': 'xml', 'data': {'data': raw, 'resid': '35'}}


def _message(sender: str, segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """构造一条协议端转发节点消息。"""
    return {
        'sender': {'nickname': sender, 'card': ''},
        'message': segments,
    }


def _record_card(
    titles: List[str],
    *,
    t_sum: str | None = '9',
) -> str:
    """构造一张最小可识别的旧式聊天记录卡片。"""
    t_sum_attr = f' tSum="{t_sum}"' if t_sum is not None else ''
    body = ''.join(f'<title>{title}</title>' for title in titles)
    return (
        f'<msg brief="[聊天记录]" action="viewMultiMsg"{t_sum_attr} serviceID="35">'
        f'<item layout="1">{body}</item></msg>'
    )


def _refuse_parser(monkeypatch) -> List[Any]:
    """把解析器换成登记调用次数的桩，返回登记列表供断言。"""
    calls: List[Any] = []

    def _spy(raw: Any) -> Any:
        calls.append(raw)
        raise xml.etree.ElementTree.ParseError('spy')

    monkeypatch.setattr(xml.etree.ElementTree, 'fromstring', _spy)
    return calls


def test_原始样本逐字渲染成卡片占位() -> None:
    """★1 真机原始 XML 的渲染必须逐字等于 D3 示例。"""
    assert render_xml_card({'data': RAW_SAMPLE_XML}) == EXPECTED_SAMPLE_RENDER


async def test_转发树带完整格式而单行预览是短标记() -> None:
    """★2 读取工具侧见完整格式，单行预览侧换短标记且不破坏 120 字预算。"""
    long_text = '这一条正文有六十个字用来占预算' * 4
    messages = [
        _message('张三', [{'type': 'text', 'data': {'text': long_text}}]),
        _message('古龙神封', [_xml_segment(RAW_SAMPLE_XML)]),
    ]

    async def _resolver(forward_id: str) -> Dict[str, Any]:
        raise AssertionError(f'渲染聊天记录卡片不应发起协议端请求：{forward_id}')

    tree = await parse_forward_content(messages, _resolver)
    card_part = tree.nodes[1].parts[0]
    assert card_part.kind == 'text'
    assert card_part.text == EXPECTED_SAMPLE_RENDER

    preview = forward_tree_preview(tree)
    assert preview == (
        f'[转发消息：张三：{long_text}｜古龙神封：[聊天记录卡片]｜共 2 条]'
    )
    assert '（图片）' not in preview
    # 若预览没有换成短标记，卡片完整渲染会把该节点挤出 120 字预算；
    # 两个节点都在场本身就证明预算仍按节点粒度在填充。
    assert len(preview) <= 120


def test_图片描述贴到真图片位置且卡片占位无内嵌半角方括号() -> None:
    """★3 卡片里的（图片）不能抢走真图片的描述，占位判断也不能被截断。"""
    render = render_xml_card({'data': RAW_SAMPLE_XML})
    assert render[0] == '[' and render[-1] == ']'
    assert '[' not in render[1:-1]
    assert ']' not in render[1:-1]

    description = '一只橘猫趴在键盘上'
    after = merge_image_descriptions(render + '[图片]', [description])
    assert after == render + f'[图片：{description}]'
    before = merge_image_descriptions('[图片]' + render, [description])
    assert before == f'[图片：{description}]' + render


def test_声明实体或文档类型的输入不进解析器(monkeypatch) -> None:
    """★4 前两种：DOCTYPE 与 ENTITY 不区分大小写整体拒绝，解析器一次都不被调用。"""
    calls = _refuse_parser(monkeypatch)
    dtd = (
        '<!DOCTYPE msg PUBLIC "-//QQ//EN" "http://example.invalid/multimsg.dtd">'
        '<msg serviceID="35"><item layout="1"><title>标题</title></item></msg>'
    )
    assert render_xml_card({'data': dtd}) == XML_PLACEHOLDER
    assert render_xml_card({'data': '<!doctype msg><msg serviceID="35"/>'}) == XML_PLACEHOLDER
    entity = (
        '<!ENTITY payload "膨胀">'
        '<msg serviceID="35"><item layout="1"><title>&payload;</title></item></msg>'
    )
    assert render_xml_card({'data': entity}) == XML_PLACEHOLDER
    assert calls == []


def test_超限与不合法的输入退回占位并留痕() -> None:
    """★4 后两种：超过 16 KB 与解析失败都得到原占位符，各记一条 warning。"""
    oversized = RAW_SAMPLE_XML.replace(
        '<hr></hr>',
        '<hr></hr><title>' + '长' * 9000 + '</title>',
    )
    with capture_logs() as events:
        assert render_xml_card({'data': oversized}) == XML_PLACEHOLDER
    too_large = [event for event in events if event.get('event') == 'xml_card_too_large']
    assert len(too_large) == 1
    assert too_large[0]['log_level'] == 'warning'

    broken = '<msg serviceID="35"><item><title>未闭合</item></msg>'
    with capture_logs() as events:
        assert render_xml_card({'data': broken}) == XML_PLACEHOLDER
    failed = [event for event in events if event.get('event') == 'xml_card_parse_failed']
    assert len(failed) == 1
    assert failed[0]['log_level'] == 'warning'
    assert failed[0]['reason']


def test_非聊天记录卡片退回占位且日志不含卡片全文() -> None:
    """5：serviceID="1" 的分享卡退回原占位符，info 日志只带三个属性值。"""
    raw = (
        '<msg serviceID="1" brief="[图文]"><item layout="1">'
        '<title>今日份新闻标题全文</title><summary>查看详情</summary>'
        '</item><source name="news"/></msg>'
    )
    with capture_logs() as events:
        assert render_xml_card({'data': raw}) == XML_PLACEHOLDER
    infos = [event for event in events if event.get('log_level') == 'info']
    assert len(infos) == 1
    assert infos[0]['event'] == 'xml_card_unrecognized'
    assert infos[0]['serviceId'] == '1'
    assert infos[0]['action'] == ''
    assert infos[0]['brief'] == '[图文]'
    rendered = json.dumps(infos[0], ensure_ascii=False)
    assert '今日份新闻标题全文' not in rendered
    assert '<msg' not in rendered


def test_带资源编号的聊天记录卡片照常渲染并留痕() -> None:
    """6：m_resid 不触发补取，只记一条 info，且不记录属性值。"""
    raw = RAW_SAMPLE_XML.replace(' tSum="2"', ' tSum="2" m_resid="RES-XYZ-123"')
    with capture_logs() as events:
        assert render_xml_card({'data': raw}) == EXPECTED_SAMPLE_RENDER
    infos = [event for event in events if event.get('log_level') == 'info']
    assert [event['event'] for event in infos] == ['chat_record_card_with_resid']
    assert 'RES-XYZ-123' not in json.dumps(events, ensure_ascii=False)


def test_预览行超过四行多出的丢弃() -> None:
    """7：预览行上限为 4 行。"""
    titles = ['标题'] + [f'第{index}行' for index in range(1, 7)]
    expected = (
        '[聊天记录卡片：标题｜第1行｜第2行｜第3行｜第4行｜共 9 条，卡片只带预览，打不开]'
    )
    assert render_xml_card({'data': _record_card(titles)}) == expected


def test_单行超过三十字截断并加省略号() -> None:
    """7：单行截到 30 字，末尾加一个省略号。"""
    long_row = '长' * 35
    expected = (
        f'[聊天记录卡片：标题｜{"长" * 30}…｜共 9 条，卡片只带预览，打不开]'
    )
    assert render_xml_card({'data': _record_card(['标题', long_row])}) == expected


def test_t_sum缺失或不是正整数时省略条数段() -> None:
    """7：tSum 缺失、为 0、非数字三种情况都省略「共 N 条，」。"""
    for t_sum in (None, '0', 'abc'):
        render = render_xml_card({'data': _record_card(['标题', '第一行'], t_sum=t_sum)})
        assert render == '[聊天记录卡片：标题｜第一行｜卡片只带预览，打不开]'
    render = render_xml_card({'data': _record_card(['标题', '第一行'], t_sum='2')})
    assert render == '[聊天记录卡片：标题｜第一行｜共 2 条，卡片只带预览，打不开]'
