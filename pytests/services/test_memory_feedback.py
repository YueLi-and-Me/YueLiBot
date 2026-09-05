"""N4 反馈纠错链路的锚点、预筛、判定与应用。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List

import pytest

from src.core.agent.profile import profiles_for_injection
from src.core.config.schema import MemoryFeedbackConfig
from src.core.memory.store import EpisodeInput, FactInput, MemoryStore
from src.core.services.maintenance.memory_feedback import (
    MemoryFeedbackService,
    has_correction_signal,
    marked_fact_ids,
    parse_judgment,
    register_prompt_entries,
)

STREAM_ID = 1
OWNER_PERSON_ID = 1
NOW = 1_800_000_000_000


class _StubProvider:
    """按预设文本回放一次模型流，并记录调用次数。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def stream(self, **_kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        self.calls += 1
        yield {'text': self.text}


def _cfg(**overrides: Any) -> MemoryFeedbackConfig:
    return MemoryFeedbackConfig(**{'enabled': True, **overrides})


def _service(db, provider, **overrides) -> MemoryFeedbackService:
    return MemoryFeedbackService(db, _cfg(**overrides), judge_provider=provider)


def _seed_fact_and_anchor(db, store: MemoryStore, *, content: str = '凌白住在深圳宝安区') -> int:
    fact_id = store.add_fact(OWNER_PERSON_ID, FactInput(content=content, kind='身份', slot='居住地'), NOW).fact_id
    register_prompt_entries(db, [(fact_id, OWNER_PERSON_ID)], STREAM_ID, NOW)
    return fact_id


class TestAnchor:
    def test_register_and_reentry_keeps_first_entry_while_pending(self, db):
        fact_id = MemoryStore(db).add_fact(
            OWNER_PERSON_ID, FactInput(content='他喜欢手冲咖啡'), NOW,
        ).fact_id
        register_prompt_entries(db, [(fact_id, OWNER_PERSON_ID)], STREAM_ID, NOW)
        register_prompt_entries(db, [(fact_id, OWNER_PERSON_ID)], STREAM_ID, NOW + 1000)

        row = db.execute(
            'SELECT entered_at, status FROM memory_feedback_pending'
        ).fetchone()
        # 待观察期间重复进入保留最早的进入时刻，窗口从第一次注入起算。
        assert row[0] == NOW and row[1] == 'pending'

    def test_reentry_after_done_starts_new_observation(self, db):
        fact_id = MemoryStore(db).add_fact(
            OWNER_PERSON_ID, FactInput(content='他喜欢手冲咖啡'), NOW,
        ).fact_id
        register_prompt_entries(db, [(fact_id, OWNER_PERSON_ID)], STREAM_ID, NOW)
        db.execute("UPDATE memory_feedback_pending SET status = 'done'")
        register_prompt_entries(db, [(fact_id, OWNER_PERSON_ID)], STREAM_ID, NOW + 5000)

        row = db.execute(
            'SELECT entered_at, status, attempts FROM memory_feedback_pending'
        ).fetchone()
        assert row[0] == NOW + 5000 and row[1] == 'pending' and row[2] == 0


class TestPrefilter:
    def test_signal_words(self):
        assert has_correction_signal('不对，我早就不住那儿了')
        assert has_correction_signal('你记错了')
        assert not has_correction_signal('今天天气真好')

    @pytest.mark.asyncio
    async def test_messages_without_signal_never_call_model(self, db):
        """★F-2：未过关键词预筛的消息不触发模型调用。"""

        store = MemoryStore(db)
        _seed_fact_and_anchor(db, store)
        store.append_message(STREAM_ID, OWNER_PERSON_ID, 'user', '今晚吃什么好呢', NOW + 1)
        provider = _StubProvider('{"negated": true, "confidence": 0.99, "corrected_content": "x"}')
        service = _service(db, provider)

        stats = await service.check_once(NOW + 60_000)

        assert provider.calls == 0
        assert stats == {'checked': 1, 'prefiltered': 0, 'judged': 0, 'applied': 0}

    @pytest.mark.asyncio
    async def test_prefilter_disabled_calls_model(self, db):
        """关闭预筛时每轮都调模型——这是它存在的意义对照。"""

        store = MemoryStore(db)
        _seed_fact_and_anchor(db, store)
        store.append_message(STREAM_ID, OWNER_PERSON_ID, 'user', '今晚吃什么好呢', NOW + 1)
        provider = _StubProvider('{"negated": false, "confidence": 0.9, "corrected_content": ""}')
        service = _service(db, provider, prefilter_enabled=False)

        stats = await service.check_once(NOW + 60_000)

        assert provider.calls == 1
        assert stats['judged'] == 1


class TestApply:
    @pytest.mark.asyncio
    async def test_correction_with_content_applies_via_superseded_by(self, db):
        """★F-3：判定成立走事实账本的 superseded_by，库里没有第二套失效标记。"""

        store = MemoryStore(db)
        old_id = _seed_fact_and_anchor(db, store)
        store.append_message(STREAM_ID, OWNER_PERSON_ID, 'user', '不对，我现在住杭州', NOW + 1)
        provider = _StubProvider(
            '{"negated": true, "confidence": 0.9, "corrected_content": "凌白现在住在杭州"}'
        )
        service = _service(db, provider)

        stats = await service.check_once(NOW + 60_000)

        assert stats['applied'] == 1
        row = db.execute('SELECT superseded_by FROM facts WHERE id = ?', (old_id,)).fetchone()
        assert row[0] is not None
        new_id = int(row[0])
        assert db.execute(
            'SELECT content FROM facts WHERE id = ?', (new_id,)
        ).fetchone()[0] == '凌白现在住在杭州'
        # facts 表上没有第二套失效标记：失效仍只有 superseded_by。
        columns = {r[1] for r in db.execute("SELECT * FROM pragma_table_info('facts')")}
        assert 'corrected' not in columns and 'invalid' not in columns
        # 旧行退出召回，新行可查。
        assert [f.id for f in store.top_facts(OWNER_PERSON_ID, 5, NOW + 2, stream_kind='direct')] == [new_id]
        # 结果表留档，标记生效。
        result = db.execute(
            'SELECT marked, new_fact_id, confidence FROM memory_feedback_results'
        ).fetchone()
        assert result[0] == 1 and result[1] == new_id and abs(result[2] - 0.9) < 1e-6

    @pytest.mark.asyncio
    async def test_below_threshold_does_not_apply(self, db):
        store = MemoryStore(db)
        old_id = _seed_fact_and_anchor(db, store)
        store.append_message(STREAM_ID, OWNER_PERSON_ID, 'user', '不对吧，好像不是深圳', NOW + 1)
        provider = _StubProvider(
            '{"negated": true, "confidence": 0.5, "corrected_content": "凌白住在杭州"}'
        )
        service = _service(db, provider)

        stats = await service.check_once(NOW + 60_000)

        assert stats['applied'] == 0
        assert db.execute(
            'SELECT superseded_by FROM facts WHERE id = ?', (old_id,)
        ).fetchone()[0] is None
        assert db.execute('SELECT COUNT(*) FROM memory_feedback_results').fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_pure_negation_marks_without_new_fact(self, db):
        """纯否定没有新说法时不造事实，只按配置写「已被纠正」标记。"""

        store = MemoryStore(db)
        old_id = _seed_fact_and_anchor(db, store)
        store.append_message(STREAM_ID, OWNER_PERSON_ID, 'user', '不对，我早就不住那儿了', NOW + 1)
        provider = _StubProvider('{"negated": true, "confidence": 0.95, "corrected_content": ""}')
        service = _service(db, provider)

        stats = await service.check_once(NOW + 60_000)

        assert stats['applied'] == 1
        assert db.execute(
            'SELECT superseded_by FROM facts WHERE id = ?', (old_id,)
        ).fetchone()[0] is None
        assert marked_fact_ids(db, [old_id]) == {old_id}
        # 标记事实被硬过滤出注入。
        from src.core.services.chat import _facts_for_prompt
        recalled = store.recall_facts(OWNER_PERSON_ID, '深圳', 5, NOW + 2, stream_kind='direct')
        assert [item.fact_id for item in _facts_for_prompt(store, OWNER_PERSON_ID, recalled, hard_filter_marked=True)] == []
        assert [item.fact_id for item in _facts_for_prompt(store, OWNER_PERSON_ID, recalled, hard_filter_marked=False)] == [old_id]

    @pytest.mark.asyncio
    async def test_mark_disabled_keeps_result_unmarked(self, db):
        store = MemoryStore(db)
        old_id = _seed_fact_and_anchor(db, store)
        store.append_message(STREAM_ID, OWNER_PERSON_ID, 'user', '不对，我早就不住那儿了', NOW + 1)
        provider = _StubProvider('{"negated": true, "confidence": 0.95, "corrected_content": ""}')
        service = _service(db, provider, mark_enabled=False)

        await service.check_once(NOW + 60_000)

        assert marked_fact_ids(db, [old_id]) == set()
        assert db.execute(
            'SELECT marked FROM memory_feedback_results'
        ).fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_model_failure_retries_then_fails(self, db):
        class _Failing:
            async def stream(self, **_kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
                raise RuntimeError('上游炸了')
                yield {}

        store = MemoryStore(db)
        _seed_fact_and_anchor(db, store)
        store.append_message(STREAM_ID, OWNER_PERSON_ID, 'user', '不对，记错了', NOW + 1)
        service = _service(db, _Failing())

        for _ in range(3):
            await service.check_once(NOW + 60_000)

        row = db.execute(
            'SELECT attempts, status FROM memory_feedback_pending'
        ).fetchone()
        assert row[0] == 3 and row[1] == 'failed'


class TestFollowUp:
    @pytest.mark.asyncio
    async def test_profile_marked_dirty_and_read_skips_stale(self, db):
        """纠错后画像置脏；开启强制刷新时读取不复用旧快照。"""

        store = MemoryStore(db)
        _seed_fact_and_anchor(db, store)
        db.execute(
            "INSERT INTO person_profile (person_id, summary, evidence_count, refreshed_at, dirty) "
            "VALUES (1, '旧快照：他住深圳', 3, 1, 0)"
        )
        store.append_message(STREAM_ID, OWNER_PERSON_ID, 'user', '不对，我现在住杭州', NOW + 1)
        provider = _StubProvider(
            '{"negated": true, "confidence": 0.9, "corrected_content": "凌白现在住在杭州"}'
        )
        service = _service(db, provider)

        await service.check_once(NOW + 60_000)

        assert db.execute(
            'SELECT dirty FROM person_profile WHERE person_id = 1'
        ).fetchone()[0] == 1
        injected = profiles_for_injection(db, [OWNER_PERSON_ID])
        assert [(item.person_id, item.confirmed, item.impression) for item in injected] == [
            (1, (), '旧快照：他住深圳')
        ]
        assert profiles_for_injection(db, [OWNER_PERSON_ID], skip_dirty=True) == []

    @pytest.mark.asyncio
    async def test_episode_queued_for_rebuild_and_blocked_from_recall(self, db):
        store = MemoryStore(db)
        _seed_fact_and_anchor(db, store)
        for i in range(4):
            store.append_message(STREAM_ID, OWNER_PERSON_ID, 'user', f'聊到住处第{i}句', NOW + i)
        episode_id = store.add_episode(
            STREAM_ID,
            EpisodeInput(
                summary='他提到凌白住在深圳宝安区，最近在调试系统。',
                cues=['深圳', '住处'],
                started_at=NOW, ended_at=NOW + 3, message_ids=[],
            ),
            NOW + 4,
        )
        store.append_message(STREAM_ID, OWNER_PERSON_ID, 'user', '不对，我现在住杭州', NOW + 10)
        provider = _StubProvider(
            '{"negated": true, "confidence": 0.9, "corrected_content": "凌白现在住在杭州"}'
        )
        service = _service(db, provider)

        await service.check_once(NOW + 60_000)

        assert db.execute(
            'SELECT needs_rebuild FROM episodes WHERE id = ?', (episode_id,)
        ).fetchone()[0] == 1
        assert [e.id for e in store.recent_episodes(STREAM_ID, 4, exclude_pending_rebuild=True)] == []
        assert [e.id for e in store.recent_episodes(STREAM_ID, 4)] == [episode_id]

    @pytest.mark.asyncio
    async def test_reconcile_expires_window_and_rebuilds_episode(self, db):
        store = MemoryStore(db)
        fact_id = _seed_fact_and_anchor(db, store)
        for i in range(4):
            store.append_message(STREAM_ID, OWNER_PERSON_ID, 'user', f'第{i}句关于深圳的讨论', NOW + i)
        episode_id = store.add_episode(
            STREAM_ID,
            EpisodeInput(
                summary='他们讨论了凌白住在深圳宝安区这件事。',
                cues=['深圳'],
                started_at=NOW, ended_at=NOW + 3,
                message_ids=[1, 2, 3, 4],
            ),
            NOW + 4,
        )
        db.execute('UPDATE episodes SET needs_rebuild = 1 WHERE id = ?', (episode_id,))
        db.commit()

        summary_provider = _StubProvider(
            '{"summary": "他们讨论了凌白现在住在杭州。", "recall_cues": ["杭州"]}'
        )
        service = MemoryFeedbackService(
            db, _cfg(), judge_provider=_StubProvider('{}'), summary_provider=summary_provider,
            bot_name='月璃', bot_personality='',
        )

        window_ms = int(12.0 * 3600_000)
        stats = await service.reconcile_once(NOW + window_ms + 1)

        assert stats['expired'] == 1
        assert stats['rebuilt'] == 1
        row = db.execute('SELECT summary, needs_rebuild FROM episodes WHERE id = ?', (episode_id,)).fetchone()
        assert row[0] == '他们讨论了凌白现在住在杭州。' and row[1] == 0
        # 旧线索被清掉，新线索进 FTS。
        assert [r[0] for r in db.execute(
            'SELECT cue FROM episode_cues WHERE episode_id = ?', (episode_id,)
        ).fetchall()] == ['杭州']
        # 过期的锚点不再进入轮询。
        assert db.execute(
            "SELECT status FROM memory_feedback_pending WHERE fact_id = ?", (fact_id,)
        ).fetchone()[0] == 'expired'


class TestParseJudgment:
    def test_plain_and_fenced_json(self):
        assert parse_judgment(
            '{"negated": true, "confidence": 0.9, "corrected_content": "他现在住杭州"}'
        ).negated is True
        fenced = parse_judgment('```json\n{"negated": false, "confidence": 0.3}\n```')
        assert fenced is not None and fenced.negated is False and fenced.corrected_content == ''

    def test_garbage_returns_none(self):
        assert parse_judgment('我觉得不对') is None
        assert parse_judgment('{"confidence": 0.9}') is None


@pytest.mark.asyncio
class TestChatAnchor:
    """锚点在回合级别的端到端：事实进提示词才登记，链路关闭时零写入。"""

    async def _run_turn(self, db, cfg) -> None:
        from src.core.config.schema import Config
        from src.core.platform_io.types import InboundMessage
        from src.core.services.chat import ChatService

        async def _noop(_channel: str, _payload: Any, _stream_id: int = 1) -> None:
            return None

        provider = _StubProvider('<say>好呀</say>')
        chat = ChatService(db, provider, None, None, _noop, cfg=cfg)
        context = chat.desktop_context
        chat.memory.add_fact(OWNER_PERSON_ID, FactInput(content='他喜欢手冲咖啡'), NOW)
        await chat.send(InboundMessage(text='聊聊咖啡', context=context))
        await chat._tick()
        await chat._inflight[context.stream.id].task

    async def test_disabled_by_default_writes_nothing(self, db):
        """★F-1：默认关闭时整条链路零写入——回合跑完待观察表是空的。"""

        from src.core.config.schema import Config
        await self._run_turn(db, Config())

        assert db.execute(
            'SELECT COUNT(*) FROM memory_feedback_pending'
        ).fetchone()[0] == 0

    async def test_enabled_registers_prompt_entries(self, db):
        """事实进入提示词后登记待观察项；关闭时同样内容一个字不写。"""

        from src.core.config.schema import Config
        await self._run_turn(db, Config(memory_feedback=MemoryFeedbackConfig(enabled=True)))

        rows = db.execute(
            'SELECT fact_id, status FROM memory_feedback_pending'
        ).fetchall()
        assert len(rows) == 1 and rows[0][1] == 'pending'
