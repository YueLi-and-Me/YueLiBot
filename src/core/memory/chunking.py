"""知识文本的清洗与切块：导入中心与存量知识的升级重切共用。

知识条目是检索与展示的最小单位，切块质量直接决定 ``consult`` 能查到什么、
查到后能读到什么。本模块把一段原始文本变成一组条目：

1. 清洗：去掉 PDF 与扫描件转换留下的页码行、紧跟页码的页眉、分隔线与换页符；
2. 分段：空行分段；PDF 在行宽处的硬换行拼回同一句；
3. 组块：单独成段的短行若后面跟着正文，视为小标题，挂到其后每一块的开头；
   正文按段累积到目标长度，超过上限的段按句切开。

对外接口是 :func:`split_into_chunks`、描述长度口径的两个常量，以及调用方用来
显式标出段落起点的 :data:`PARAGRAPH_BREAK`。
被 ``import_center.py``（导入资料）与数据库迁移 ``v34_to_v35.py``（存量知识按本
规则重切）调用；不依赖数据库。

两条判定刻意从严，宁可漏判也不误伤：

- 硬换行只在「上一行够长且没有句末标点、下一行不是列表或标题」时拼接。
  歌词、诗句、清单的行都短，不会被拼成一长串；
- 小标题必须单独成段、足够短、无句末标点，且后面紧跟一段足够长的正文。
  连续的短段（歌词、目录）互相不构成标题关系。
"""

from __future__ import annotations

from typing import List, Optional

import re

# 显式段落分隔符：调用方已知段落起点时（例如 PDF 转换留下的段首标记），把它单独
# 放一行。换页清理会去掉页码两侧的空行，但不会去掉它——空行在换页处多半是排版
# 产物，这个分隔符则一定是真实的段落边界，去掉它会让小标题与正文并成一段。
# 取私用区字符：正常文本里不会出现。不能用 U+2029 段落分隔符，Python 把它算作
# 空白，逐行 strip 时会被吃掉，标记随之失效。
PARAGRAPH_BREAK = '\ue000'
# 组块的目标长度（字符）：正文按段累积，达到它即成一块。与 consult 的单条展示
# 上限配合——一块应能被完整读到，而不是只露出开头。
CHUNK_TARGET_CHARS = 300
# 单块上限（字符）：超过它的段按句切开，仍超长的句按行、再按字数硬切。
CHUNK_MAX_CHARS = 500
# 小标题的长度上限（字符）。
_HEADING_MAX_CHARS = 30
# 小标题后面紧跟的正文至少要有这么长，才认定前者是标题而不是又一行短句。
_HEADING_BODY_MIN_CHARS = 40
# 硬换行拼接的行长下限：短于它的行视为有意断行（歌词、清单、标题）。
_WRAP_MIN_LINE_CHARS = 20
# 末块短于它时并入前一块（同一小标题下、合并后不超上限）。
_TAIL_MERGE_CHARS = 60
# 页眉判定：紧跟页码行出现、且重复至少这么多次的短行。
_RUNNING_HEADER_MIN_REPEATS = 3

# 页码行：「- 12 -」「— 12 —」「第 12 页」「=====第12页=====」「12/300」。
_PAGE_LINE = re.compile(
    r'^\s*(?:[-—–]\s*\d+\s*[-—–]|第\s*\d+\s*页|=+\s*第\s*\d+\s*页\s*=+|\d+\s*/\s*\d+)\s*$'
)
# 分隔线：同一类符号连续三个以上，整行只有它们。
_RULE_LINE = re.compile(r'^\s*([=\-—_*~·•◆■□─━])\1{2,}\s*$')
# 只有一个网址的行：转载出处与链接列表，不是知识内容。
_URL_LINE = re.compile(r'^\s*(?:https?://|www\.)\S+\s*$')
# 小标题须含至少一个汉字或西文字母：页码残留（「45」）与编号不算标题。
_HEADING_WORD = re.compile(r'[A-Za-z\u3400-\u9fff]')
# Markdown 代码围栏行：知识条目按纯文本展示，围栏只剩噪声。
_FENCE_LINE = re.compile(r'^\s*```\w*\s*$')
# 句末标点：以它们结尾的行是完整的一句，不与下一行拼接。
_SENTENCE_END = '。！？!?；;…：:”’」』）)】》'
# 结构行：列表项、Markdown 标题与表格行，永不与上一行拼接。
_STRUCTURAL = re.compile(r'^\s*(?:#{1,6}\s|[-*+•·]\s|\d+[.、)）]|[（(]\d+[)）]|[一二三四五六七八九十]+、|\|)')
# 按句切分：在句末标点之后断开，标点留在前一句。
_SENTENCE_SPLIT = re.compile(r'(?<=[。！？!?；;…])')
# 西文字母与数字：硬换行两侧都是它们时拼接处补空格。
_ASCII_WORD = re.compile(r'[A-Za-z0-9]')


def _clean_lines(text: str) -> List[str]:
    """去掉页码行、紧跟页码的重复页眉、分隔线与代码围栏，返回逐行列表。

    :param text: 原始文本。
    :return: 清洗后的行，保留空行作为段落边界。
    副作用：无。
    """
    lines = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x0c', '\n').split('\n')
    lines = [line.rstrip() for line in lines]
    # 页眉只认紧跟页码行（中间可隔空行）的短行：全文重复的短句（歌词副歌、清单项）
    # 不在页码旁边，不会被当成页眉删掉。
    candidates: dict[str, int] = {}
    for index, line in enumerate(lines):
        if not _PAGE_LINE.match(line):
            continue
        for follower in lines[index + 1:index + 4]:
            stripped = follower.strip()
            if stripped:
                if len(stripped) <= _HEADING_MAX_CHARS:
                    candidates[stripped] = candidates.get(stripped, 0) + 1
                break
    headers = {line for line, count in candidates.items() if count >= _RUNNING_HEADER_MIN_REPEATS}
    cleaned: List[str] = []
    after_page_break = False
    for line in lines:
        stripped = line.strip()
        if _PAGE_LINE.match(line) or stripped in headers:
            # 换页处的空行是排版产物，不是段落边界：句子常在页尾断开、下页接续，
            # 保留这些空行会把同一句切进两个段落。前后的空行一并去掉。
            while cleaned and not cleaned[-1]:
                cleaned.pop()
            after_page_break = True
            continue
        if _RULE_LINE.match(line) or _FENCE_LINE.match(line) or _URL_LINE.match(line):
            continue
        if not stripped and after_page_break:
            continue
        after_page_break = False
        cleaned.append(stripped)
    return cleaned


def _joins_next(length: int, last_char: str, following: str) -> bool:
    """判断上一行与下一行是否是被硬换行切开的同一句。

    :param length: 上一行（含已拼上的续行）的长度。
    :param last_char: 上一行的末字。
    :param following: 下一行。
    """
    if length < _WRAP_MIN_LINE_CHARS or not following:
        return False
    if last_char in _SENTENCE_END or _STRUCTURAL.match(following):
        return False
    return True


def _paragraphs(lines: List[str]) -> List[str]:
    """按空行分段，并把段内被硬换行切开的行拼回去。

    拼接时两侧都是 CJK 字符则直接相连，否则补一个空格；不拼接的行保留换行。

    :param lines: :func:`_clean_lines` 的产物。
    :return: 非空段落列表。
    副作用：无。
    """
    paragraphs: List[str] = []
    # 段内片段先收进列表、段落结束时一次拼接。逐行拼接字符串并回切出末行，耗时随段长
    # 平方增长：没有空行的成篇资料（迁移知识整篇约 300 万字）会因此卡住数十秒。
    pieces: List[str] = []
    # 段内最后一行（含已拼上的续行）的长度与末字：判断能否拼接下一行只需要这两项。
    last_length = 0
    last_char = ''
    for line in lines:
        if not line or line == PARAGRAPH_BREAK:
            if pieces:
                paragraphs.append(''.join(pieces))
                pieces = []
            continue
        if pieces and _joins_next(last_length, last_char, line):
            # 只有两侧都是西文字母或数字时才补空格（英文单词被换行隔开）；
            # 中文与全角标点在原文里本就不带空格。
            glue = ' ' if _ASCII_WORD.match(last_char) and _ASCII_WORD.match(line[0]) else ''
            pieces.append(f'{glue}{line}')
            last_length += len(glue) + len(line)
        else:
            pieces.append(f'\n{line}' if pieces else line)
            last_length = len(line)
        last_char = line[-1]
    if pieces:
        paragraphs.append(''.join(pieces))
    return paragraphs


def _is_heading(paragraph: str, following: Optional[str]) -> bool:
    """单独成段、短、无句末标点、且后面紧跟足够长正文的段落是小标题。"""
    if '\n' in paragraph:
        return False
    if paragraph.lstrip().startswith('#'):
        return True
    if len(paragraph) > _HEADING_MAX_CHARS or paragraph[-1] in '。！!；;，,、':
        return False
    if len(paragraph) < 2 or not _HEADING_WORD.search(paragraph):
        return False
    return following is not None and len(following) >= _HEADING_BODY_MIN_CHARS


def _split_long(paragraph: str) -> List[str]:
    """把超过上限的段按句、再按行、最后按字数切成不超上限的片段。"""
    pieces: List[str] = []
    for sentence in _SENTENCE_SPLIT.split(paragraph):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= CHUNK_MAX_CHARS:
            pieces.append(sentence)
            continue
        for line in sentence.split('\n'):
            while len(line) > CHUNK_MAX_CHARS:
                pieces.append(line[:CHUNK_MAX_CHARS])
                line = line[CHUNK_MAX_CHARS:]
            if line.strip():
                pieces.append(line.strip())
    return pieces


def split_into_chunks(text: str) -> List[str]:
    """把一段原始文本清洗后切成知识条目。

    小标题挂在其下每一块的开头（``标题\\n正文``），使每块脱离上下文仍可读、
    可被标题词检索到。正文按段累积到 :data:`CHUNK_TARGET_CHARS` 成块，单块不超过
    :data:`CHUNK_MAX_CHARS` 加标题长度；只有标题、没有正文的段落不单独成块。

    :param text: 原始文本。
    :return: 条目列表，按原文顺序；全部去除首尾空白，空文本返回空列表。
    副作用：无。
    """
    paragraphs = _paragraphs(_clean_lines(text))
    # 每块记住自己的小标题：末块只并入同一标题下的前一块，标题语境不会串。
    blocks: List[tuple[str, str]] = []
    heading = ''
    body = ''
    body_heading = ''

    def flush() -> None:
        nonlocal body
        if not body:
            return
        if (
            blocks and len(body) < _TAIL_MERGE_CHARS
            and blocks[-1][0] == body_heading
            and len(blocks[-1][1]) + len(body) + 1 <= CHUNK_MAX_CHARS
        ):
            blocks[-1] = (body_heading, f'{blocks[-1][1]}\n{body}')
        else:
            blocks.append((body_heading, body))
        body = ''

    for index, paragraph in enumerate(paragraphs):
        following = paragraphs[index + 1] if index + 1 < len(paragraphs) else None
        if _is_heading(paragraph, following):
            flush()
            heading = paragraph.lstrip('#').strip()
            continue
        pieces = _split_long(paragraph) if len(paragraph) > CHUNK_MAX_CHARS else [paragraph]
        for piece in pieces:
            if body and (body_heading != heading or len(body) + len(piece) + 1 > CHUNK_MAX_CHARS):
                flush()
            if not body:
                body_heading = heading
                body = piece
            else:
                body = f'{body}\n{piece}'
            if len(body) >= CHUNK_TARGET_CHARS:
                flush()
    flush()
    chunks = [f'{title}\n{content}' if title else content for title, content in blocks]
    return [chunk.strip() for chunk in chunks if chunk.strip()]
