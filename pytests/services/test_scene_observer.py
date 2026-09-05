"""观察 Agent（场景画像）验收。

三层：解析严格性（枚举封闭、不编默认场景）、触发与并发（按条数节流、同 stream 不重入、
失败保留旧画像），以及注入（只进群聊系统提示词、关闭时整块省略）。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import pytest

from src.core.agent.observer import (
    ATMOSPHERES,
    TOPIC_MAX_CHARS,
    SceneSnapshot,
    parse_scene,
)
from src.core.config.schema import Config
from src.core.memory.store import MemoryStore
from src.core.platform_io.types import DeliveryReceipt, InboundMessage, OutboundMessage
from src.core.services.chat import ChatService


class _JsonProvider:
    """按脚本返回整段文本的替身提供方。"""

    def __init__(self, payloads: List[str]) -> None:
        self.payloads = payloads
        self.calls = 0
        self.prompts: List[str] = []

    async def stream(self, messages=None, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        index = self.calls
        self.calls += 1
        for message in messages or []:
            if message.get('role') == 'system':
                self.prompts.append(message['content'])
        yield {'text': self.payloads[min(index, len(self.payloads) - 1)]}


class _Broker:
    async def dispatch(self, message: OutboundMessage) -> DeliveryReceipt:
        return DeliveryReceipt(
            platform=message.stream.platform,
            stream_id=message.stream.id,
            external_message_ids=['x'],
        )


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


def _config(*, refresh: int = 3) -> Config:
    config = Config()
    config.bot.name = '月璃'
    config.conversation_agent.mode = 'off'
    config.group_chat.scene_refresh_messages = refresh
    return config


def _group(registry, external_id: str = '86420'):
    return registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id=external_id,
        sender_external_id='97531',
        sender_nickname='账号昵称',
        sender_group_card='小李',
        first_seen_at=1_000_000,
    )


_SCENE_JSON = '{"topic": "在聊周末去哪吃", "atmosphere": "热闹"}'


class TestParsing:
    def test_valid_payload(self) -> None:
        scene = parse_scene(_SCENE_JSON, observed_message_id=42)
        assert scene.topic == '在聊周末去哪吃'
        assert scene.atmosphere == '热闹'
        assert scene.observed_message_id == 42

    def test_unknown_atmosphere_is_rejected(self) -> None:
        """气氛是封闭枚举：自造一个不能被接受，也不许悄悄改成邻近值。"""
        with pytest.raises(ValueError):
            parse_scene('{"topic": "x", "atmosphere": "阴森"}', 1)

    def test_non_json_is_rejected_not_defaulted(self) -> None:
        """解析失败不编一个默认场景——那会让她按从未观察到的气氛说话。"""
        with pytest.raises(ValueError):
            parse_scene('群里在聊吃饭，气氛不错', 1)

    def test_overlong_topic_is_rejected(self) -> None:
        long_topic = '吃' * (TOPIC_MAX_CHARS + 1)
        with pytest.raises(ValueError):
            parse_scene(f'{{"topic": "{long_topic}", "atmosphere": "热闹"}}', 1)

    def test_oversized_output_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_scene('x' * 5000, 1)

    def test_every_atmosphere_parses(self) -> None:
        for atmosphere in ATMOSPHERES:
            assert parse_scene(
                f'{{"topic": "t", "atmosphere": "{atmosphere}"}}', 1,
            ).atmosphere == atmosphere


class TestSnapshotRoundTrip:
    def test_round_trip(self) -> None:
        scene = SceneSnapshot(topic='t', atmosphere='平淡', observed_message_id=7)
        assert SceneSnapshot.from_dict(scene.to_dict()) == scene

    def test_unreadable_payloads_become_none(self) -> None:
        """读不懂的旧画像等于没有画像，不修正只丢弃。"""
        for payload in (
            None,
            'not-a-dict',
            {'topic': 't'},
            {'topic': 't', 'atmosphere': '已废弃的枚举值', 'observedMessageId': 1},
            {'topic': '', 'atmosphere': '平淡', 'observedMessageId': 1},
            {'topic': 't', 'atmosphere': '平淡', 'observedMessageId': 'x'},
        ):
            assert SceneSnapshot.from_dict(payload) is None


class TestTriggerAndCache:
    def _chat(self, db, provider, *, refresh: int = 3) -> ChatService:
        return ChatService(
            db, provider, None, provider, _noop,
            cfg=_config(refresh=refresh), broker=_Broker(),
        )

    async def test_below_threshold_does_not_observe(self, db) -> None:
        provider = _JsonProvider([_SCENE_JSON])
        chat = self._chat(db, provider, refresh=5)
        context = _group(chat._registry)
        chat.memory.append_message(context.stream.id, context.person.id, 'user', '甲', 1)

        chat._schedule_scene_observation(context)

        assert provider.calls == 0
        assert chat.memory.read_json(chat._scene_key(context.stream.id), None) is None

    async def test_observation_writes_snapshot(self, db) -> None:
        provider = _JsonProvider([_SCENE_JSON])
        chat = self._chat(db, provider, refresh=2)
        context = _group(chat._registry)
        for text in ('今晚吃啥', '火锅？', '太热了'):
            chat.memory.append_message(context.stream.id, context.person.id, 'user', text, 1)

        await chat._run_scene_observation(context)

        stored = SceneSnapshot.from_dict(
            chat.memory.read_json(chat._scene_key(context.stream.id), None)
        )
        assert stored is not None and stored.topic == '在聊周末去哪吃'
        # 观察读的是群聊历史，提示词里必须带上说话人前缀。
        assert '小李: 今晚吃啥' in provider.prompts[0]

    async def test_failure_keeps_previous_snapshot(self, db) -> None:
        """观察是附加背景，失败只记日志、保留旧画像，绝不影响对话。"""
        provider = _JsonProvider(['这不是 JSON'])
        chat = self._chat(db, provider, refresh=1)
        context = _group(chat._registry)
        chat.memory.append_message(context.stream.id, context.person.id, 'user', '甲', 1)
        previous = SceneSnapshot(topic='旧话题', atmosphere='平淡', observed_message_id=1)
        chat.memory.write_json(chat._scene_key(context.stream.id), previous.to_dict())

        result = await chat._run_scene_observation(context)

        assert result == 'scene_observation_failed'
        assert SceneSnapshot.from_dict(
            chat.memory.read_json(chat._scene_key(context.stream.id), None)
        ) == previous

    async def test_concurrent_observation_is_skipped(self, db) -> None:
        provider = _JsonProvider([_SCENE_JSON])
        chat = self._chat(db, provider, refresh=1)
        context = _group(chat._registry)
        chat.memory.append_message(context.stream.id, context.person.id, 'user', '甲', 1)
        chat._observing.add(context.stream.id)

        chat._schedule_scene_observation(context)

        assert provider.calls == 0


class TestPromptInjection:
    def _chat(self, db, *, refresh: int = 3) -> ChatService:
        provider = _JsonProvider([_SCENE_JSON])
        return ChatService(
            db, provider, None, provider, _noop,
            cfg=_config(refresh=refresh), broker=_Broker(),
        )

    def test_group_scene_is_exposed(self, db) -> None:
        chat = self._chat(db)
        context = _group(chat._registry)
        chat.memory.write_json(
            chat._scene_key(context.stream.id),
            SceneSnapshot(topic='在聊显卡', atmosphere='认真', observed_message_id=3).to_dict(),
        )

        assert chat._scene_for_prompt(context) == ('在聊显卡', '认真')

    def test_desktop_never_gets_a_scene(self, db) -> None:
        """私聊与桌面不存在「群里在聊什么」这个问题。"""
        chat = self._chat(db)
        chat.memory.write_json(
            chat._scene_key(chat.desktop_context.stream.id),
            SceneSnapshot(topic='x', atmosphere='平淡', observed_message_id=1).to_dict(),
        )

        assert chat._scene_for_prompt(chat.desktop_context) is None

    def test_disabled_observer_omits_the_block(self, db) -> None:
        chat = self._chat(db, refresh=0)
        context = _group(chat._registry)
        chat.memory.write_json(
            chat._scene_key(context.stream.id),
            SceneSnapshot(topic='x', atmosphere='平淡', observed_message_id=1).to_dict(),
        )

        # 关闭周期刷新只是不调度群聊后台观察；私聊即时情景分析仍共用这个 Agent。
        assert chat._scene_observer is not None
        assert chat._scene_for_prompt(context) is None

    def test_scene_block_renders_into_system_prompt(self) -> None:
        from src.core.agent.prompt import build_system_prompt

        with_scene = build_system_prompt(
            name='月璃', birthday='', personality='p', reply_style='r',
            scene=('在聊显卡', '认真'),
        )
        without = build_system_prompt(
            name='月璃', birthday='', personality='p', reply_style='r',
        )
        assert '# 群里现在的情况' in with_scene
        assert '在聊显卡' in with_scene
        assert '# 群里现在的情况' not in without


class TestMessageCountAfter:
    def test_counts_only_later_messages(self, db) -> None:
        store = MemoryStore(db)
        first = store.append_message(1, 1, 'user', '甲', 1)
        store.append_message(1, 1, 'user', '乙', 2)
        store.append_message(1, None, 'assistant', '丙', 3)

        assert store.message_count_after(1, 0) == 3
        assert store.message_count_after(1, first) == 2
        assert store.message_count_after(2, 0) == 0
