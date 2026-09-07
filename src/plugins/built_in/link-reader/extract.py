"""链接插件的编码判定与通用正文抽取。

decode_body 按响应头、HTML 元数据和中文编码顺序严格解码；extract_content
用标准库 HTMLParser 保留页面元信息、排除非正文区域。仅被同包 plugin.py 调用。
"""

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
import codecs
import re


# 只嗅探开头 4 KiB，避免为查编码扫描整篇文章。
SNIFF_BYTES = 4096
CHARSET_PATTERN = re.compile(r'charset\s*=\s*[\"\x27]?\s*([^\s;\"\x27/>]+)', re.I)
IGNORED_TAGS = frozenset({
    'script', 'style', 'noscript', 'svg', 'head', 'nav', 'header', 'footer',
    'aside', 'form', 'iframe', 'template',
})
BLOCK_TAGS = frozenset({
    'address', 'article', 'blockquote', 'br', 'dd', 'div', 'dl', 'dt', 'h1', 'h2',
    'h3', 'h4', 'h5', 'h6', 'hr', 'li', 'main', 'ol', 'p', 'pre', 'section',
    'table', 'td', 'th', 'tr', 'ul',
})


class DecodeError(ValueError):
    """所有允许的编码候选都无法严格解码，不能输出替换字符冒充正文。"""


class _MetaSniffer(HTMLParser):
    """只收集编码声明，latin-1 输入允许任意原始字节经过嗅探。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.encodings: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        """接受直接 charset 或 http-equiv=content-type 两种 meta 声明。"""
        if tag != 'meta':
            return
        values = dict(attrs)
        charset = values.get('charset')
        if charset:
            self.encodings.append(charset)
        if (values.get('http-equiv') or '').lower() == 'content-type':
            match = CHARSET_PATTERN.search(values.get('content') or '')
            if match:
                self.encodings.append(match[1])


def decode_body(body: bytes, content_type: str, *, truncated: bool = False) -> str:
    """按固定优先级严格解码有限正文，未知编码名跳过。

    :param body: 抓取保留的原始字节。
    :param content_type: 含可选 charset 的响应头。
    :param truncated: 是否因抓取上限截断，默认 False；为 True 时不提交尾部半个字符。
    :return: 成功解码的文本；空字节返回空字符串。
    :raises DecodeError: 响应头、meta、UTF-8、GB18030 全部不可用。
    """
    candidates: List[str] = []
    match = CHARSET_PATTERN.search(content_type)
    if match:
        candidates.append(match[1])
    sniffer = _MetaSniffer()
    try:
        sniffer.feed(body[:SNIFF_BYTES].decode('latin-1'))
    except Exception:
        # HTMLParser 可在损坏的声明处抛错；编码声明可能仍已被收集。
        # 丢弃全部候选会让有可靠 meta 的中文页走错编码，因此保留已读元数据。
        pass
    candidates.extend(sniffer.encodings)
    candidates.extend(('utf-8', 'gb18030'))
    for encoding in candidates:
        try:
            # UTF-8 的替换解码会把中文页面伪装成成功，造成不可诊断的乱码；
            # 所有候选都必须严格解码，失败才进入下一个明确候选。
            if truncated:
                # 字节闸门可能停在中文字符中间；完整解码会误判编码并尝试其它字符集。
                # 增量解码只暂存末尾半个字符，其余位置仍严格校验，避免把截断误报为乱码。
                decoder = codecs.getincrementaldecoder(encoding)(errors='strict')
                decoded = decoder.decode(body, final=False)
                if not isinstance(decoded, str):
                    continue
                return decoded
            return body.decode(encoding, errors='strict')
        except (LookupError, UnicodeError, ValueError):
            continue
    raise DecodeError('网页编码不可读：响应头、meta、UTF-8 与 GB18030 均无法解码')


@dataclass(frozen=True)
class ExtractedContent:
    """网页元信息及正文；缺失字段统一为空字符串。"""

    title: str = ''
    site_name: str = ''
    description: str = ''
    body: str = ''


class _ContentParser(HTMLParser):
    """保留元信息、隔离不可见区域，并按块级边界收集正文。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: Dict[str, str] = {}
        self.title: List[str] = []
        self.text: List[str] = []
        self.ignored: List[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        """元信息从 head 中单独读取；正文过滤栈不记录 void 标签。"""
        values = dict(attrs)
        if tag == 'meta' and not any(item != 'head' for item in self.ignored):
            key = (values.get('property') or values.get('name') or '').lower()
            content = values.get('content')
            if content:
                self.meta.setdefault(key, content.strip())
        if tag == 'title' and not any(item != 'head' for item in self.ignored):
            self.in_title = True
        if tag in IGNORED_TAGS:
            self.ignored.append(tag)
        if not self.ignored and tag in BLOCK_TAGS:
            self.text.append('\n')

    def handle_endtag(self, tag: str) -> None:
        """在对应忽略区闭合时恢复正文；不匹配的结束标签不改变状态。"""
        if tag == 'title':
            self.in_title = False
        if tag in self.ignored:
            index = len(self.ignored) - 1 - self.ignored[::-1].index(tag)
            del self.ignored[index:]
        if not self.ignored and tag in BLOCK_TAGS:
            self.text.append('\n')

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        """自闭合忽略区不能泄漏状态，否则后续全部正文会被丢弃。"""
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        """标题与正文分开积累，忽略区的数据不进入正文。"""
        if self.in_title:
            self.title.append(data)
        elif not self.ignored:
            self.text.append(data)


def extract_content(text: str, content_type: str) -> ExtractedContent:
    """抽取 HTML 元信息与正文；其它已允许的文本类型保留原文。

    :param text: 已成功解码的字符串。
    :param content_type: HTTP 内容类型，可含参数。
    :return: 字段均为字符串的抽取结果。损坏 HTML 保留已解析内容，不抛异常。
    """
    if content_type.partition(';')[0].strip().lower() not in {'text/html', 'application/xhtml+xml'}:
        return ExtractedContent(body=text)
    parser = _ContentParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # 畸形 HTML 会触发解析器内部断言；已经取得的内容仍有价值。
        # 原文回退会把脚本和隐藏指令重新带入正文，因此只保留已过滤的片段。
        pass
    lines: List[str] = []
    for raw in ''.join(parser.text).splitlines():
        line = raw.strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return ExtractedContent(
        title=parser.meta.get('og:title', ''.join(parser.title).strip()),
        site_name=parser.meta.get('og:site_name', ''),
        description=parser.meta.get('og:description', parser.meta.get('description', '')),
        body='\n'.join(lines),
    )
