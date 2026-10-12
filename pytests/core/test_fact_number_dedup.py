"""事实去重的数字判定：只差数字的两句不算同一件事，不触及数字的改写照常合并。"""

from __future__ import annotations

import sqlite3

import pytest

from src.core.memory import curate
from src.core.memory.similarity import exact_key, is_same_fact
from src.core.memory.store import FactInput, MemoryStore

NOW = 1_800_000_000_000
OWNER_PERSON_ID = 1

RELEASE_2024 = '凌白在2024年10月12日发布了yuelibot的0.1.5版本，上一次发版是9月19日'
RELEASE_2026 = '凌白在2026年10月12日发布了yuelibot的0.1.5版本，上一次发版是9月19日'
CATS_TWO = '凌白家里养了两只猫，一只是橘猫一只是狸花'
CATS_THREE = '凌白家里养了三只猫，一只是橘猫一只是狸花'


@pytest.fixture
def store():
    db = sqlite3.connect(':memory:')
    store = MemoryStore(db)
    yield store
    db.close()


class TestIsSameFact:
    def test_only_year_differs(self):
        assert not is_same_fact(RELEASE_2026, RELEASE_2024)
        assert not is_same_fact(RELEASE_2024, RELEASE_2026)

    def test_only_arabic_quantity_differs(self):
        assert not is_same_fact(
            '凌白每月在AI模型订阅上的相关花费接近600元',
            '凌白每月在AI模型订阅上的相关花费接近800元',
        )

    def test_only_chinese_quantity_differs(self):
        assert not is_same_fact(CATS_TWO, CATS_THREE)
        assert not is_same_fact(
            '「 」的作息是晚上十点睡、凌晨四点起床',
            '「 」的作息是晚上十一点睡、凌晨四点起床',
        )

    def test_separator_between_digits_is_kept_in_key(self):
        assert exact_key('yuelibot的0.1.5版本') != exact_key('yuelibot的0.15版本')
        assert exact_key('预算约为1.4万元') != exact_key('预算约为14万元')
        assert exact_key('2024-10-12') == exact_key('2024/10/12')
        assert not is_same_fact('凌白发布了yuelibot的0.1.5版本', '凌白发布了yuelibot的0.15版本')

    def test_rewording_without_touching_numbers_still_merges(self):
        assert is_same_fact(CATS_TWO, '凌白家里养了两只猫，一只是橘猫，一只是狸花。')
        assert is_same_fact('2294904033的昵称是南浊。', '2294904033昵称是南浊。')
        assert is_same_fact(
            '1442990726 开空调时会同时打开窗户，因为不喜欢一直吹空调且懒得反复开关窗',
            '1442990726 开空调时会同时打开窗户，因为不喜欢吹空调且懒得反复开关窗',
        )
        assert is_same_fact(
            '凌白同时使用四个AI模型进行编程与对话，每月相关花费接近600元',
            '每月相关花费接近600元，凌白同时使用四个AI模型进行编程与对话',
        )


class TestWritePaths:
    def test_number_only_difference_creates_new_row(self, store):
        old = store.add_fact(OWNER_PERSON_ID, FactInput(content=RELEASE_2024, kind='经历'), NOW)
        new = store.add_fact(OWNER_PERSON_ID, FactInput(content=RELEASE_2026, kind='经历'), NOW + 1)

        assert new.created and new.fact_id != old.fact_id
        assert store.fact_row(old.fact_id)['hit_count'] == 0

    def test_supersede_with_number_only_difference_retires_old_row(self, store):
        old = store.add_fact(OWNER_PERSON_ID, FactInput(content=CATS_TWO, kind='经历'), NOW)
        new = store.add_fact(
            OWNER_PERSON_ID,
            FactInput(content=CATS_THREE, kind='经历', supersedes=old.fact_id, actor='n4'),
            NOW + 1,
        )

        assert new.created and new.superseded == old.fact_id
        assert store.fact_row(old.fact_id)['superseded_by'] == new.fact_id

    def test_manual_replace_accepts_number_correction(self, store):
        old = store.add_fact(OWNER_PERSON_ID, FactInput(content=RELEASE_2024, kind='经历'), NOW)

        written = curate.replace_fact(store, old.fact_id, RELEASE_2026, NOW + 1)

        assert written.created and written.fact_id != old.fact_id
        assert store.fact_row(old.fact_id)['superseded_by'] == written.fact_id
        assert store.fact_row(written.fact_id)['content'] == RELEASE_2026
