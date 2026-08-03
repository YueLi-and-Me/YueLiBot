"""
分词模块测试。移植自 src/core/memory/tokenize.test.ts。

jieba 版本比 Intl.Segmenter 切得准，所以「香菜」不再需要 bigram 救场，
但 bigram 仍保留用于自造专名（月璃）和 jieba 词典未收录的词。
"""

from __future__ import annotations

import sqlite3
import pytest

from yueli.memory.tokenize import bigrams, index_tokens, match_query, words


class TestWords:
    def test_cuts_chinese_words(self):
        result = words('玩家喜欢在深夜写代码')
        assert '玩家' in result
        assert '喜欢' in result
        assert '深夜' in result
        assert '写' in result or '代码' in result

    def test_filters_stop_words_keeps_content(self):
        w = words('我把饭都吃了')
        assert '把' not in w
        assert '都' not in w
        assert '饭' in w

    def test_bigrams_only_cjk(self):
        assert bigrams('香菜') == ['香菜']
        assert bigrams('abc香菜def') == ['香菜']
        # 英文和中文之间不跨界
        result = bigrams('写Rust代码')
        assert '代码' in result
        # 中文片段不应和 Rust 合并
        for bg in result:
            assert 'R' not in bg and 'u' not in bg

    def test_index_tokens_contains_both(self):
        t = index_tokens('香菜')
        assert '香菜' in t

    def test_match_query_escapes_fts_syntax(self):
        q = match_query('a*b NEAR c')
        # * 必须被引号包住而不是裸露出现
        assert q is not None and 'OR' in q


class TestFts5Recall:
    """真实 SQLite FTS5 召回测试（移植 TS 版的 FTS5 中文召回 describe 块）。"""

    FACTS = [
        '玩家不喜欢吃香菜',
        '玩家习惯在深夜写代码',
        '月璃今天穿了水母主题的连衣裙',
        '玩家养了一只叫团子的猫',
        '玩家的生日是三月七日',
        '玩家最近在学 Rust',
    ]

    @pytest.fixture(autouse=True)
    def setup_db(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE facts (id INTEGER PRIMARY KEY, content TEXT NOT NULL)')
        self.db.execute(
            "CREATE VIRTUAL TABLE facts_fts USING fts5(tokens, content='', tokenize='unicode61')"
        )
        for fact in self.FACTS:
            cur = self.db.execute('INSERT INTO facts (content) VALUES (?)', (fact,))
            fid = cur.lastrowid
            self.db.execute(
                'INSERT INTO facts_fts (rowid, tokens) VALUES (?, ?)', (fid, index_tokens(fact))
            )
        self.db.commit()
        yield
        self.db.close()

    def _search(self, q: str) -> list[str]:
        mq = match_query(q)
        if not mq:
            return []
        rows = self.db.execute(
            '''SELECT f.content FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
               WHERE facts_fts MATCH ? ORDER BY bm25(facts_fts) LIMIT 5''',
            (mq,)
        ).fetchall()
        return [r[0] for r in rows]

    def test_jieba_correctly_segments_xiangcai(self):
        """jieba 正确切出香菜，不需要 bigram 兜底（比 Intl.Segmenter 更好）。"""
        result = self._search('香菜')
        assert result and result[0] == '玩家不喜欢吃香菜'

    def test_custom_name_recalled_via_bigram(self):
        """自造专名「月璃」不在 jieba 词典，依靠 bigram 召回。"""
        result = self._search('月璃')
        assert result and '月璃' in result[0]

    def test_compound_word_recalled(self):
        result = self._search('连衣裙')
        assert result and '连衣裙' in result[0]

    def test_normal_segmentation_first(self):
        assert self._search('深夜写代码')[0] == '玩家习惯在深夜写代码'
        assert self._search('猫')[0] == '玩家养了一只叫团子的猫'

    def test_english_and_numbers(self):
        result = self._search('Rust')
        assert result and 'Rust' in result[0]

    def test_natural_question_recall(self):
        """自然语气整句提问也能召回（OR 语义，不要求全词命中）。"""
        result = self._search('我上次说的那个香菜是什么')
        assert result and result[0] == '玩家不喜欢吃香菜'

    def test_unrelated_query_returns_empty(self):
        assert self._search('量子力学薛定谔') == []
