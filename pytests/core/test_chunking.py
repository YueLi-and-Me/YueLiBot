"""知识切块验收。

覆盖通用切块器（``memory/chunking.py``）的清洗、拼接、小标题与组块规则。
"""

from __future__ import annotations

from src.core.memory.chunking import (
    CHUNK_MAX_CHARS,
    CHUNK_TARGET_CHARS,
    PARAGRAPH_BREAK,
    split_into_chunks,
)

_BODY = '这是一段足够长的正文，用来说明小标题后面确实跟着内容，而不是又一行短句。'


class TestCleaning:
    def test_page_numbers_and_running_header_removed(self) -> None:
        text = '\n'.join([
            '第一句正文写到这里还没有结束，因为排版在行宽处断开了所以继续到下一',
            '- 12 -',
            '',
            '最全的生活百科全书',
            '行才结束。',
            '- 13 -',
            '最全的生活百科全书',
            '另一段。',
            '- 14 -',
            '最全的生活百科全书',
        ])
        chunks = split_into_chunks(text)
        joined = '\n'.join(chunks)
        assert '- 12 -' not in joined and '最全的生活百科全书' not in joined
        assert '继续到下一行才结束。' in joined, '跨页断开的句子应拼回一句'

    def test_repeated_line_away_from_page_markers_is_kept(self) -> None:
        """只删紧跟页码的页眉；歌词副歌这类全文重复的短句不受影响。"""
        lyrics = '\n'.join(['河边的风 在吹着头发飘动'] * 4)
        assert split_into_chunks(lyrics) == [lyrics]

    def test_rules_fences_and_url_lines_removed(self) -> None:
        text = '```\n=====\n正文第一句。\nhttp://example.com/a/b\n-----\n```'
        assert split_into_chunks(text) == ['正文第一句。']


class TestJoining:
    def test_hard_wrap_joined_without_space_for_cjk(self) -> None:
        text = '另外，血流滞缓，排毒管道不通畅，多种毒素易留存体内，再加之寒冷的气候会减低机体的抵抗力，\n这样就常常形成一种病理状态。'
        assert split_into_chunks(text) == [
            '另外，血流滞缓，排毒管道不通畅，多种毒素易留存体内，再加之寒冷的气候会减低机体的抵抗力，'
            '这样就常常形成一种病理状态。'
        ]

    def test_hard_wrap_joined_with_space_for_latin(self) -> None:
        text = 'The quick brown fox jumps over the lazy\ndog every single morning.'
        assert split_into_chunks(text) == ['The quick brown fox jumps over the lazy dog every single morning.']

    def test_short_lines_and_lists_not_joined(self) -> None:
        text = '1. 第一项说明的文字已经很长了但是没有句号结尾\n2. 第二项'
        assert split_into_chunks(text) == [text]


class TestHeadings:
    def test_heading_prefixed_to_its_chunks(self) -> None:
        text = f'进补最好先排毒\n\n{_BODY}'
        assert split_into_chunks(text) == [f'进补最好先排毒\n{_BODY}']

    def test_heading_requires_following_body(self) -> None:
        """后面不是长正文的短行不算标题，按正文保留。"""
        text = '第一行\n\n第二行'
        assert split_into_chunks(text) == ['第一行\n第二行']

    def test_heading_needs_a_word(self) -> None:
        """页码残留与单字不算标题。"""
        assert split_into_chunks(f'45\n\n{_BODY}') == [f'45\n{_BODY}']
        assert split_into_chunks(f'例\n\n{_BODY}') == [f'例\n{_BODY}']

    def test_paragraph_break_survives_page_cleanup(self) -> None:
        """显式段落分隔符不会被换页清理去掉，标题与正文不会并成一段。"""
        text = '\n'.join([
            PARAGRAPH_BREAK, '怎样才是有气质的女人？', '- 1207 -', PARAGRAPH_BREAK,
            '有人说过，要对一个不漂亮的女人作评价，如果她还算知书达理，可称之为有气质的女人。',
        ])
        chunks = split_into_chunks(text)
        assert chunks == [
            '怎样才是有气质的女人？\n有人说过，要对一个不漂亮的女人作评价，如果她还算知书达理，可称之为有气质的女人。'
        ]

    def test_heading_does_not_leak_into_next_section(self) -> None:
        text = f'第一节标题\n\n{_BODY}\n\n第二节标题\n\n{_BODY}第二节。'
        chunks = split_into_chunks(text)
        assert chunks[0].startswith('第一节标题\n')
        assert chunks[1].startswith('第二节标题\n')


class TestSizing:
    def test_short_paragraphs_merged_up_to_target(self) -> None:
        paragraphs = [f'第{i}段的内容写得比较完整。' for i in range(60)]
        chunks = split_into_chunks('\n\n'.join(paragraphs))
        assert len(chunks) < len(paragraphs) / 5
        assert all(len(chunk) <= CHUNK_MAX_CHARS for chunk in chunks)
        assert all(len(chunk) >= CHUNK_TARGET_CHARS * 0.5 for chunk in chunks[:-1])

    def test_unpunctuated_wall_is_hard_cut(self) -> None:
        wall = '字' * (CHUNK_MAX_CHARS * 2 + 10)
        chunks = split_into_chunks(wall)
        assert ''.join(chunks) == wall
        assert all(len(chunk) <= CHUNK_MAX_CHARS for chunk in chunks)

    def test_empty_text(self) -> None:
        assert split_into_chunks('  \n\n ') == []

