"""聊天服务热应用配置后，真实决策、模型采样和名字匹配使用新值。"""

from typing import Any, AsyncIterator, Dict, List

import sqlite3

from src.core.config.schema import Config, RegexRuleConfig
from src.core.observe.store import event_store
from src.core.services.chat import ChatService
from src.core.platform_io.types import ConversationContext, InboundMessage


class _Provider:
    """记录请求参数并输出固定协议，验证实际调用而非只检查缓存字段。"""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: List[Dict[str, Any]] = []

    async def stream(
        self, messages: List[Dict[str, Any]] | None = None, **kwargs: Any,
    ) -> AsyncIterator[Dict[str, str]]:
        self.calls.append({'messages': messages, **kwargs})
        yield {'text': self.response}


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


def _group(chat: ChatService) -> ConversationContext:
    return chat._registry.resolve_inbound(
        platform='qq', stream_kind='group', stream_external_id='test-group',
        sender_external_id='test-person', sender_nickname='测试用户',
        sender_group_card='', first_seen_at=1,
    )


async def test_reload_enables_decision_and_updates_trigger_sampling_and_names(db: sqlite3.Connection) -> None:
    initial = Config()
    provider = _Provider('<decision action="silent" mind="测试想法" reasons="others_conversation"/>')
    chat = ChatService(db, provider, None, None, _noop, cfg=initial)
    context = _group(chat)
    fresh = initial.model_copy(deep=True)
    fresh.bot.name = '新名字'
    fresh.bot.aliases = ['新别名']
    fresh.conversation_agent.mode = 'enabled'
    fresh.conversation_agent.trigger_mode = 'reply_necessity'
    fresh.conversation_agent.max_cognitive_rounds = 0
    fresh.generation.chat.temperature = 0.23
    fresh.personality.regex_rules = [RegexRuleConfig(pattern='^猫来了$', reaction='仅供正文')]

    chat.apply_config(fresh)

    assert chat.bot_names() == ('新名字', '新别名')
    assert chat.conversation_trigger_mode == 'reply_necessity'
    assert chat.regex_rule_matches(context, '猫来了')
    await chat.send(InboundMessage(text='猫来了', context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task
    assert len(provider.calls) == 1
    assert provider.calls[0]['temperature'] == 0.23
    assert '仅供正文' not in str(provider.calls[0]['messages'])
    assert event_store.search(kinds=['action_decision']).events[0]['eventStatus'] == 'silent_by_choice'


async def test_reload_updates_split_model_sampling_and_preserves_cooldown(db: sqlite3.Connection) -> None:
    initial = Config()
    initial.conversation_agent.mode = 'enabled'
    initial.conversation_agent.tool_calling = False
    planner = _Provider('<decision action="silent" mind="测试想法" reasons="others_conversation"/>')
    replyer = _Provider('<say>测试回复</say>')
    chat = ChatService(
        db, planner, None, None, _noop, cfg=initial,
        planner_provider=planner, replyer_provider=replyer,
    )
    context = _group(chat)
    key = ('^猫来了$', context.stream.id)
    chat._regex_rule_cooldowns[key] = 123
    fresh = initial.model_copy(deep=True)
    fresh.personality.regex_rules = [RegexRuleConfig(pattern=key[0], reaction='仅供正文')]
    fresh.generation.planner.temperature = 0.17
    fresh.generation.planner.max_tokens = 700
    fresh.conversation_agent.max_cognitive_rounds = 0

    chat.apply_config(fresh)
    chat.apply_config(fresh)
    await chat.send(InboundMessage(text='猫来了', context=context))
    await chat._tick()
    await chat._inflight[context.stream.id].task

    assert len(planner.calls) == 1
    assert planner.calls[0]['temperature'] == 0.17
    assert planner.calls[0]['max_tokens'] == 700
    assert replyer.calls == []
    assert chat._regex_rule_cooldowns == {key: 123}
