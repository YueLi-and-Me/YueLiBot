"""人物画像信任分级：确凿档投影、证据指纹短路与两档注入呈现（★T-1~★T-6）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncIterator, Dict

import json
import sqlite3

from src.core.agent import profile
from src.core.agent.profile import (
    InjectionProfile,
    evidence_fingerprint,
    mark_dirty,
    parse_confirmed,
    profiles_for_injection,
    refresh_profiles,
    render_evidence,
)
from src.core.agent.prompt import build_system_prompt
from src.core.memory import similarity
from src.core.memory.store import EpisodeInput, FactInput, MemoryStore
from src.core.observe.store import event_store

STREAM_ID = 1
PERSON_ID = 1
NOW = 1_800_000_000_000


class _StubProvider:
    """按预设文本回放一次模型流，并记录调用次数；可切换为故障模式。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.fail = False

    async def stream(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError('模型故障')
        yield {'text': self.text}


def _refresh(db: sqlite3.Connection, provider: _StubProvider, now: int, **kwargs: Any):
    return refresh_profiles(
        db, provider, bot_name='月璃', temperature=0.3, max_tokens=None, now=now, **kwargs,
    )


def _add_person(db: sqlite3.Connection, person_id: int) -> None:
    """person_profile 有指向 persons 的外键，直接插画像行前先把人建出来。"""

    db.execute(
        "INSERT INTO persons (id, kind, first_seen_at) VALUES (?, 'contact', 1)",
        (person_id,),
    )
    db.commit()


def _seed_evidence(db: sqlite3.Connection) -> Dict[str, int]:
    """种三条有效事实（两条带槽位）与一条情节，返回各类行 ID。"""

    store = MemoryStore(db)
    ids = {
        'nickname': store.add_fact(
            PERSON_ID, FactInput(content='她叫凌白', kind='身份', slot='昵称'), NOW,
        ).fact_id,
        'job': store.add_fact(
            PERSON_ID, FactInput(content='她是插画师', kind='身份', slot='职业'), NOW,
        ).fact_id,
        'taste': store.add_fact(
            PERSON_ID, FactInput(content='她不吃香菜', kind='偏好'), NOW,
        ).fact_id,
    }
    message_id = store.append_message(STREAM_ID, PERSON_ID, 'user', '今晚聊到很晚', NOW)
    ids['episode'] = store.add_episode(
        STREAM_ID,
        EpisodeInput(
            summary='一起熬夜赶稿', cues=['熬夜'],
            started_at=NOW - 3_600_000, ended_at=NOW, message_ids=[message_id],
        ),
        NOW,
    )
    return ids


def _profile_row(db: sqlite3.Connection, person_id: int = PERSON_ID) -> sqlite3.Row | tuple:
    return db.execute(
        'SELECT summary, confirmed, evidence_fingerprint, dirty FROM person_profile '
        'WHERE person_id = ?',
        (person_id,),
    ).fetchone()


class TestConfirmedTier:
    async def test_model_fabrication_never_enters_confirmed_tier(self, db):
        """★T-1：模型产出（含材料里没有的虚构）只落在印象档，确凿档一个标点都不含。"""

        _seed_evidence(db)
        mark_dirty(db, [PERSON_ID], NOW)
        provider = _StubProvider('她很安静。虚构：她家养了一只叫雪球的猫。')

        await _refresh(db, provider, NOW + 1)

        summary, confirmed_raw, _, dirty = _profile_row(db)
        assert dirty == 0
        assert '雪球' in summary  # 虚构内容确实进了印象档——防线不在这一侧
        entries = parse_confirmed(confirmed_raw)
        assert entries, '确凿档应有账本投影'
        assert all('雪球' not in entry.content for entry in entries)
        assert all('虚构' not in entry.content for entry in entries)

    async def test_confirmed_entries_trace_to_real_fact_ids(self, db):
        """★T-2：确凿档每条都能用 id 对应到一条真实 fact，且正文与账本逐字一致。"""

        ids = _seed_evidence(db)
        # 一条被取代的事实：失效条目不进确凿档。
        old = MemoryStore(db).add_fact(
            PERSON_ID, FactInput(content='她住深圳', kind='身份', slot='居住地'), NOW,
        ).fact_id
        current = MemoryStore(db).add_fact(
            PERSON_ID,
            FactInput(content='她住杭州', kind='身份', slot='居住地', supersedes=old),
            NOW,
        ).fact_id
        mark_dirty(db, [PERSON_ID], NOW)

        await _refresh(db, _StubProvider('她很安静。'), NOW + 1)

        entries = parse_confirmed(_profile_row(db)[1])
        assert {entry.fact_id for entry in entries} == {
            ids['nickname'], ids['job'], ids['taste'], current,
        }
        for entry in entries:
            row = db.execute(
                'SELECT content, slot, kind, active, superseded_by FROM facts WHERE id = ?',
                (entry.fact_id,),
            ).fetchone()
            assert row is not None
            assert entry.content == row[0]
            assert entry.label == (row[1] or row[2])
            assert row[3] == 1 and row[4] is None

    def test_parse_confirmed_treats_corrupt_cache_as_empty(self, db):
        """缓存行损坏按空档处理，不能拖垮注入；重建交给下一次刷新。"""

        assert parse_confirmed('') == ()
        assert parse_confirmed('not json') == ()
        assert parse_confirmed('{"oops": 1}') == ()
        assert parse_confirmed('[{"fact_id": 1}]') == ()
        _add_person(db, 9)
        db.execute(
            "INSERT INTO person_profile (person_id, summary, confirmed, dirty) "
            "VALUES (9, '印象还在', 'not json', 0)"
        )
        injected = profiles_for_injection(db, [9])
        assert [(item.person_id, item.confirmed, item.impression) for item in injected] == [
            (9, (), '印象还在')
        ]


class TestFingerprintShortCircuit:
    async def test_unchanged_fingerprint_skips_model_call(self, db):
        """★T-3：指纹没变时不调模型（只推进时间戳、清脏位）；证据变了才调。"""

        _seed_evidence(db)
        provider = _StubProvider('她很安静。')
        mark_dirty(db, [PERSON_ID], NOW)
        await _refresh(db, provider, NOW + 1)
        assert provider.calls == 1
        first = _profile_row(db)
        assert first[2] != '' and first[3] == 0

        # 仅置脏、证据未变：跳过模型，脏位照清，正文与指纹不动，发跳过事件。
        event_store.clear()
        mark_dirty(db, [PERSON_ID], NOW + 2)
        cleared = await _refresh(db, provider, NOW + 3)
        assert provider.calls == 1
        assert cleared == 1
        second = _profile_row(db)
        assert second[0] == first[0] and second[1] == first[1] and second[2] == first[2]
        assert second[3] == 0
        skips = event_store.search(kinds=['profile_refresh_skipped']).events
        assert len(skips) == 1 and skips[0]['personId'] == PERSON_ID

        # 新事实到账 → 指纹变化 → 重新调用模型。
        MemoryStore(db).add_fact(
            PERSON_ID, FactInput(content='她最近在学版画', kind='经历'), NOW + 4,
        )
        mark_dirty(db, [PERSON_ID], NOW + 4)
        await _refresh(db, provider, NOW + 5)
        assert provider.calls == 2

    async def test_no_impression_is_a_result_not_a_failure(self, db):
        """模型按契约输出空串（材料不足）要落档存指纹，不能当故障每轮重试。"""

        _seed_evidence(db)
        provider = _StubProvider('   ')
        mark_dirty(db, [PERSON_ID], NOW)
        await _refresh(db, provider, NOW + 1)
        assert provider.calls == 1
        summary, _, fingerprint, dirty = _profile_row(db)
        assert summary == '' and fingerprint != '' and dirty == 0

        mark_dirty(db, [PERSON_ID], NOW + 2)
        await _refresh(db, provider, NOW + 3)
        assert provider.calls == 1  # 指纹命中，不再为空印象反复调用模型

    async def test_model_failure_keeps_dirty_and_writes_nothing(self, db):
        """模型故障：脏位保留、确凿档与指纹不落——两档必须对应同一份证据。"""

        _seed_evidence(db)
        provider = _StubProvider('不会到达')
        provider.fail = True
        mark_dirty(db, [PERSON_ID], NOW)
        await _refresh(db, provider, NOW + 1)
        assert provider.calls == 1
        summary, confirmed, fingerprint, dirty = _profile_row(db)
        assert (summary, confirmed, fingerprint, dirty) == ('', '', '', 1)

        provider.fail = False
        await _refresh(db, provider, NOW + 2)
        assert provider.calls == 2
        _, confirmed, fingerprint, dirty = _profile_row(db)
        assert confirmed != '' and fingerprint != '' and dirty == 0

    async def test_empty_evidence_never_calls_model(self, db):
        """没有任何本地证据的人不发模型请求；空证据指纹同样参与短路。"""

        provider = _StubProvider('不会到达')
        mark_dirty(db, [PERSON_ID], NOW)
        await _refresh(db, provider, NOW + 1)
        assert provider.calls == 0
        assert _profile_row(db)[3] == 0

        mark_dirty(db, [PERSON_ID], NOW + 2)
        await _refresh(db, provider, NOW + 3)
        assert provider.calls == 0

    def test_fingerprint_covers_participating_ids(self, db):
        """指纹只随参与生成的 fact / episode id 集合变化，与顺序和正文措辞无关。"""

        ids = _seed_evidence(db)
        evidence = render_evidence(db, PERSON_ID)
        again = render_evidence(db, PERSON_ID)
        assert evidence_fingerprint(evidence) == evidence_fingerprint(again)

        store = MemoryStore(db)
        store.add_fact(PERSON_ID, FactInput(content='她怕打雷', kind='偏好'), NOW + 1)
        changed = render_evidence(db, PERSON_ID)
        assert evidence_fingerprint(changed) != evidence_fingerprint(evidence)

        # 情节不参与该人证据时指纹不因其变化（他人情节不计入）。
        other_message = store.append_message(STREAM_ID, 2, 'user', '旁人的话', NOW + 2)
        store.add_episode(
            STREAM_ID,
            EpisodeInput(
                summary='别人的情节', cues=[], started_at=NOW + 1, ended_at=NOW + 2,
                message_ids=[other_message],
            ),
            NOW + 2,
        )
        assert evidence_fingerprint(render_evidence(db, PERSON_ID)) == evidence_fingerprint(changed)
        assert ids['episode'] is not None


class TestInjectionRendering:
    async def test_two_tiers_distinguishable_in_prompt(self, db):
        """★T-4：两档在最终提示词里各有标记，确凿条目与印象正文分别出现。"""

        _seed_evidence(db)
        mark_dirty(db, [PERSON_ID], NOW)
        await _refresh(db, _StubProvider('她很安静，画画到很晚。'), NOW + 1)

        injected = profiles_for_injection(db, [PERSON_ID])
        prompt = build_system_prompt(
            name='月璃', birthday='', personality='观察细节',
            reply_style='简短接话', now=datetime(2032, 7, 15, 12, 5),
            impressions=injected,
        )

        assert '记着的：' in prompt
        assert '印象：她很安静，画画到很晚。' in prompt
        assert '- 昵称：她叫凌白' in prompt
        assert '- 偏好：她不吃香菜' in prompt
        assert '逐条有记录可查' in prompt

    def test_impression_only_profile_renders_without_confirmed_marker(self, db):
        """存量画像（只有印象档）照常注入，不出现空的确凿标记。"""

        _add_person(db, 3)
        db.execute(
            "INSERT INTO person_profile (person_id, summary, confirmed, dirty) "
            "VALUES (3, '旧版自由文本画像', '', 0)"
        )
        prompt = build_system_prompt(
            name='月璃', birthday='', personality='观察细节',
            reply_style='简短接话', now=datetime(2032, 7, 15, 12, 5),
            impressions=profiles_for_injection(db, [3]),
        )
        assert '印象：旧版自由文本画像' in prompt
        assert '记着的：' not in prompt

    def test_confirmed_only_profile_renders_without_impression_marker(self):
        """只有确凿档（印象为空）时整块只有「记着的」。"""

        item = InjectionProfile(
            person_id=4,
            confirmed=parse_confirmed(
                json.dumps([{'fact_id': 1, 'label': '昵称', 'content': '他叫阿七'}])
            ),
            impression='',
        )
        prompt = build_system_prompt(
            name='月璃', birthday='', personality='观察细节',
            reply_style='简短接话', now=datetime(2032, 7, 15, 12, 5),
            impressions=[item],
        )
        assert '记着的：' in prompt and '- 昵称：他叫阿七' in prompt
        assert '印象：' not in prompt


class TestDerivedCacheRebuild:
    async def test_drop_table_contents_and_rebuild_loses_nothing(self, db):
        """★T-5：整表清空后按现有刷新路径重建，两档与指纹逐字节一致。"""

        _seed_evidence(db)
        mark_dirty(db, [PERSON_ID], NOW)
        provider = _StubProvider('她很安静，画画到很晚。')
        await _refresh(db, provider, NOW + 1)
        before = _profile_row(db)
        assert before[1] != '' and before[2] != ''

        db.execute('DELETE FROM person_profile')
        db.commit()
        assert _profile_row(db) is None

        mark_dirty(db, [PERSON_ID], NOW + 2)
        await _refresh(db, provider, NOW + 3)
        after = _profile_row(db)
        assert provider.calls == 2
        assert after[:3] == before[:3]  # 印象、确凿档、指纹完全重建


class TestSimilarityNormalizeCleanup:
    def test_public_normalize_is_gone_and_module_still_works(self):
        """★T-6：\\p{P} 版公共 normalize 已删除；去重与相似度走可用的私有实现。"""

        assert not hasattr(similarity, 'normalize')
        assert similarity.is_same_fact('他住在深圳！', '他住在深圳')
        assert not similarity.is_same_fact('他住在深圳', '她去了北京')
        scores = similarity.similarity('她叫凌白', '她叫凌白')
        assert scores == {'char': 1.0, 'bigram': 1.0}
        assert similarity.exact_key(' 你好，世界！ ') == similarity.exact_key('你好世界')
