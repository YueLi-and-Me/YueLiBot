"""第 6 步（a）规划器与消息唤醒验收。"""

from __future__ import annotations

from dataclasses import MISSING
from datetime import datetime
from hashlib import sha256
from inspect import getsource
from time import monotonic
from typing import Any, AsyncIterator

import asyncio

import pytest

from src.core.agent import prompt as prompt_module
from src.core.agent.action import (
    ActionContext,
    AlwaysReplyPolicy,
    PresenceActionPolicy,
    ReplyDecision,
    TurnAction,
    TurnPlanner,
)
from src.core.agent.prompt import build_system_prompt
from src.core.config.schema import Config
from src.core.platform_io.types import InboundMessage
from src.core.prompts import registry as prompt_registry
from src.core.prompts.registry import (
    CHAT_PROACTIVE_TEMPLATE_IDS,
    CHAT_SYSTEM_COMPONENTS,
    CHAT_SYSTEM_TEMPLATE_IDS,
    configure_prompts,
    get_prompt,
    list_prompts,
    prompt_history,
    prompt_metadata,
    render_chat_system,
    reset_prompts_for_tests,
    update_prompt,
)
from src.core.services.chat import ChatService
from src.core.services.dev.replay import replay_event


class _WakeProvider:
    """记录正文模型调用，并暴露请求开始信号。"""

    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()

    async def stream(self, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        self.calls += 1
        self.started.set()
        yield {'text': '<say emotion="normal">收到</say>'}


class _RejectReplayProvider:
    """若旧事件错误进入模型调用则立即让验收失败。"""

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, **_kwargs: Any) -> AsyncIterator[dict[str, str]]:
        self.calls += 1
        yield {'text': '不应调用'}


class _CaptureReplayProvider:
    """记录重放时真正提交给模型的消息。"""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[dict[str, str]]:
        self.messages = kwargs['messages']
        yield {'text': '重放完成'}


class _CaptureActionPolicy:
    """记录决策上下文，并返回测试指定的动作。"""

    def __init__(self, action: TurnAction) -> None:
        self.action = action
        self.contexts: list[ActionContext] = []
        self.decided = asyncio.Event()

    @property
    def decision_source(self) -> str:
        """返回验收事件使用的稳定来源。"""
        return type(self).__name__

    async def decide(self, context: ActionContext) -> TurnAction:
        """保存未经修改的决策上下文并返回预设动作。"""
        self.contexts.append(context)
        self.decided.set()
        return self.action


async def _noop(_channel: str, _payload: Any, _stream_id: int) -> None:
    return None


@pytest.mark.parametrize(
    ('text', 'expected'),
    (
        ('这是短输入', 'brief'),
        ('长输入' * 40, 'long'),
    ),
)
async def test_turn_planner_selects_reply_length(text: str, expected: str) -> None:
    planner = TurnPlanner(AlwaysReplyPolicy())

    action = await planner.decide(ActionContext(
        turn_id=1,
        stream_id=1,
        messages=({'role': 'user', 'content': text},),
        batch_text=text,
    ))

    assert action.action == 'reply'
    assert action.length == expected


@pytest.mark.parametrize(
    ('texts', 'expected'),
    (
        (('长输入' * 26, '在吗'), 'long'),
        (('第一句', '第二句'), 'brief'),
    ),
)
async def test_turn_planner_counts_contiguous_user_batch(
    texts: tuple[str, ...],
    expected: str,
) -> None:
    planner = TurnPlanner(AlwaysReplyPolicy())

    action = await planner.decide(ActionContext(
        turn_id=1,
        stream_id=1,
        messages=tuple({'role': 'user', 'content': text} for text in texts),
        batch_text='\n'.join(texts),
    ))

    assert action.length == expected


async def test_turn_planner_ignores_adjacent_previous_sender_batch() -> None:
    planner = TurnPlanner(AlwaysReplyPolicy())
    current_text = '乙' * 30
    isolated = await planner.decide(ActionContext(
        turn_id=1,
        stream_id=1,
        messages=({'role': 'user', 'content': current_text},),
        batch_text=current_text,
    ))
    after_other_sender = await planner.decide(ActionContext(
        turn_id=2,
        stream_id=1,
        messages=(
            {'role': 'user', 'content': '甲' * 60},
            {'role': 'user', 'content': current_text},
        ),
        batch_text=current_text,
    ))

    assert after_other_sender.length == isolated.length


async def test_turn_planner_short_batch_stays_brief_after_silent_rounds() -> None:
    planner = TurnPlanner(AlwaysReplyPolicy())

    action = await planner.decide(ActionContext(
        turn_id=1,
        stream_id=1,
        messages=tuple(
            {'role': 'user', 'content': text}
            for text in ('甲' * 30, '乙' * 30, '丙' * 30, '当前短输入')
        ),
        batch_text='当前短输入',
    ))

    assert action.length == 'brief'


async def test_group_speaker_prefix_length_does_not_change_reply_length() -> None:
    planner = TurnPlanner(AlwaysReplyPolicy())
    batch_text = '第一行\n第二行: 保留冒号后的正文\n第三行'
    short_name_action = await planner.decide(ActionContext(
        turn_id=1,
        stream_id=1,
        messages=({'role': 'user', 'content': f'甲: {batch_text}'},),
        batch_text=batch_text,
    ))
    long_name_action = await planner.decide(ActionContext(
        turn_id=2,
        stream_id=1,
        messages=({
            'role': 'user',
            'content': f'{"很长的群成员显示名" * 5}: {batch_text}',
        },),
        batch_text=batch_text,
    ))
    direct_action = await planner.decide(ActionContext(
        turn_id=3,
        stream_id=2,
        messages=({'role': 'user', 'content': batch_text},),
        batch_text=batch_text,
    ))

    assert short_name_action.length == 'brief'
    assert long_name_action.length == short_name_action.length
    assert direct_action.length == short_name_action.length


def test_action_context_requires_batch_text_and_drops_stream_kind() -> None:
    assert 'stream_kind' not in ActionContext.__dataclass_fields__
    assert ActionContext.__dataclass_fields__['batch_text'].default is MISSING


@pytest.mark.parametrize(
    ('action', 'expected_render_calls', 'expected_provider_calls'),
    (
        (TurnAction(action='reply', reason='验收回复', length='brief'), 1, 1),
        (TurnAction(action='silent', reason='验收静默'), 0, 0),
    ),
)
async def test_decision_point_only_renders_for_actual_reply(
    db,
    monkeypatch: pytest.MonkeyPatch,
    action: TurnAction,
    expected_render_calls: int,
    expected_provider_calls: int,
) -> None:
    """回复只渲染最终提示词一次，静默回合完全不渲染。"""
    provider = _WakeProvider()
    policy = _CaptureActionPolicy(action)
    chat = ChatService(
        db,
        provider,
        provider,
        provider,
        _noop,
        cfg=Config(),
        action_policy=policy,
    )
    original_render = chat._render_prepared_context
    render_calls = 0

    def count_render(*args: Any, **kwargs: Any) -> list[dict]:
        nonlocal render_calls
        render_calls += 1
        return original_render(*args, **kwargs)

    monkeypatch.setattr(chat, '_render_prepared_context', count_render)

    await chat.send(InboundMessage(text='验收决策点渲染次数', context=chat.desktop_context))
    await chat._tick()
    await policy.decided.wait()
    inflight = chat._inflight.get(chat.desktop_context.stream.id)
    if inflight is not None:
        await inflight.task

    assert render_calls == expected_render_calls
    assert provider.calls == expected_provider_calls
    assert len(policy.contexts) == 1


async def test_action_context_receives_uncropped_raw_group_history(db) -> None:
    """决策上下文逐条保留群聊原始历史，不拼 system，也不按模型预算裁剪。"""
    policy = _CaptureActionPolicy(TurnAction(action='silent', reason='只检查上下文'))
    provider = _WakeProvider()
    chat = ChatService(
        db,
        provider,
        provider,
        provider,
        _noop,
        cfg=Config(),
        action_policy=policy,
    )
    context = chat._registry.resolve_inbound(
        platform='qq',
        stream_kind='group',
        stream_external_id='验收群',
        sender_external_id='验收成员',
        sender_nickname='账号昵称',
        sender_group_card='群名片',
        first_seen_at=1_800_000_000_000,
    )
    long_text = '旧消息' * 4_001
    chat.memory.append_message(
        context.stream.id,
        context.person.id,
        'user',
        long_text,
        1_800_000_000_000,
    )

    await chat.send(InboundMessage(text='当前消息', context=context))
    await chat._tick()
    await policy.decided.wait()

    assert len(policy.contexts) == 1
    messages = policy.contexts[0].messages
    # 断言重点是原文一字未裁；行首的发言时刻与群名片都是显示前缀，用 endswith 跳过。
    assert len(messages) == 2
    assert all(message['role'] == 'user' for message in messages)
    assert messages[0]['content'].endswith(f'群名片: {long_text}')
    assert messages[1]['content'].endswith('群名片: 当前消息')
    assert all(message['role'] != 'system' for message in messages)


def test_reply_length_template_mapping_has_single_declaration() -> None:
    """篇幅枚举到模板 ID 的映射只由提示词注册表声明。"""
    mapping = getattr(prompt_registry, 'REPLY_LENGTH_TEMPLATE_IDS', None)

    assert mapping == {
        'brief': 'chat.length.brief',
        'long': 'chat.length.long',
    }
    assert tuple(mapping.values()) == tuple(prompt_registry.CHAT_SYSTEM_VARIANT_COMPONENTS)
    assert tuple(mapping.values()) == prompt_registry.TEMPLATE_IDS[3:5]
    assert not hasattr(prompt_registry, 'CHAT_LENGTH_TEMPLATE_IDS')
    assert not hasattr(prompt_module, '_REPLY_LENGTH_TEMPLATE_IDS')


@pytest.mark.parametrize(
    ('reply_length', 'expected_text_hash', 'expected_prompt_hash'),
    (
        # 基准随 17da24e 更新一次：chat.discipline.md 第一条由折行改为单行，
        # 进入模型的文本因此少了一个换行与三个缩进空格。已逐条验证过这是唯一差异
        # ——把新文本在该处还原成折行后，三个基准哈希与更新前逐字节相同。
        # 模板指纹随「共处群注入」更新一次：chat.system.md 新增 {{shared_groups}}
        # 占位符，模板字节改变所以 promptHash 变化；空注入时渲染文本逐字节不变，
        # 三个正文哈希保持原值——这正是「无注入时零差异」的直接证据。
        (None, '7b3b2c0d38bb897102145bb2cad33d39b7602fa167099a3fc9850689c3942ec2', '2182f42a'),
        ('brief', 'b6c6e59e68cb4599d78385f3eaea49dba4a4f84309c781c82d93352e185fddf8', '1442d6d1'),
        ('long', 'f974875ebf5645112c1db96e2773b026211ed990c503e764eb24a6487df76d11', '24965cca'),
    ),
)
def test_k_cleanup_keeps_prompt_text_and_hash_identical(
    reply_length: str | None,
    expected_text_hash: str,
    expected_prompt_hash: str,
) -> None:
    """K-1/K-2 只能清理内部契约，不能改变进入模型的任何字符。"""
    prompt = build_system_prompt(
        now=datetime(2026, 8, 13, 17, 30),
        name='月璃',
        birthday='',
        personality='测试人格',
        reply_style='自然回复',
        reply_length=reply_length,
    )
    variant_ids = (
        (f'chat.length.{reply_length}',)
        if reply_length is not None
        else ()
    )

    assert sha256(prompt.encode('utf-8')).hexdigest() == expected_text_hash
    assert prompt_metadata(
        'chat.system',
        (*CHAT_SYSTEM_TEMPLATE_IDS, *variant_ids),
    )['promptHash'] == expected_prompt_hash


def test_chat_reads_declared_decision_source_without_type_fallback() -> None:
    source = getsource(ChatService._start_turn)

    assert 'decisionSource=action_policy.decision_source' in source
    assert 'isinstance(action_policy' not in source


async def test_inner_policies_return_narrow_decision_without_length() -> None:
    context = ActionContext(turn_id=1, stream_id=1, messages=(), batch_text='')
    always = await AlwaysReplyPolicy().decide(context)
    presence = await PresenceActionPolicy(
        base_probability=0.0,
        decay_strength=3.0,
        window_minutes=10,
        assistant_reply_count_since=lambda _stream_id, _since: 0,
        message_count_since=lambda _stream_id, _since: 0,
        probability_draw=lambda: 0.5,
        clock=lambda: 1_000_000,
    ).decide(context)

    assert always == ReplyDecision(should_reply=True, reason='默认策略始终回复')
    assert presence.should_reply is False
    assert not isinstance(always, TurnAction)
    assert not isinstance(presence, TurnAction)


def test_turn_action_still_rejects_contradictory_length_combinations() -> None:
    with pytest.raises(ValueError, match='静默动作不能声明回复篇幅'):
        TurnAction(action='silent', reason='矛盾动作', length='long')
    with pytest.raises(ValueError, match='回复动作必须声明有效篇幅'):
        TurnAction(action='reply', reason='缺少篇幅')


def test_reply_length_is_injected_as_natural_language() -> None:
    render_params: dict[str, dict[str, str]] = {}

    prompt = build_system_prompt(
        name='月璃',
        birthday='',
        personality='测试人格',
        reply_style='自然回复',
        reply_length='brief',
        render_params=render_params,
    )

    instruction = (
        '简短回复：允许句子残缺、奇怪表达、倒装和省略，符合口语和省力回复习惯。\n'
        '整轮合计二三十个字即可，不要自行展开成完整段落。'
    )
    assert instruction in prompt
    assert render_params['chat.system']['length'] == (
        f'\n\n# 这一轮的篇幅\n{instruction}'
    )
    assert render_params['chat.length.brief'] == {}
    assert '{{instruction}}' not in prompt


def test_empty_reply_length_leaves_no_heading_or_extra_blank_line() -> None:
    prompt = build_system_prompt(
        name='月璃',
        birthday='',
        personality='测试人格',
        reply_style='自然回复',
        reply_length=None,
    )

    assert '# 这一轮的篇幅' not in prompt
    assert '# 说话风格\n自然回复\n\n# 事实纪律' in prompt


def test_reply_length_block_keeps_heading_and_position() -> None:
    prompt = build_system_prompt(
        name='月璃',
        birthday='',
        personality='测试人格',
        reply_style='自然回复',
        reply_length='brief',
    )

    assert (
        '# 说话风格\n自然回复\n\n'
        '# 这一轮的篇幅\n'
        '简短回复：允许句子残缺、奇怪表达、倒装和省略，符合口语和省力回复习惯。\n'
        '整轮合计二三十个字即可，不要自行展开成完整段落。\n\n'
        '# 事实纪律'
    ) in prompt


async def test_message_wakes_poll_loop_without_waiting_for_heartbeat(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.services.chat.service as chat_module

    monkeypatch.setattr(chat_module, 'CHAT_POLL_INTERVAL_S', 5.0)
    provider = _WakeProvider()
    chat = ChatService(db, provider, provider, provider, _noop, cfg=Config())
    await chat.startup()
    await asyncio.sleep(0)
    started_at = monotonic()

    try:
        await chat.send(InboundMessage(text='立即唤醒', context=chat.desktop_context))
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        elapsed = monotonic() - started_at
        inflight = chat._inflight[chat.desktop_context.stream.id]
        await asyncio.wait_for(inflight.task, timeout=1)
    finally:
        await chat.shutdown()

    assert provider.calls == 1
    assert elapsed < 2


def test_length_variant_only_changes_selected_hash_and_creates_history(tmp_path) -> None:
    configure_prompts(tmp_path)
    try:
        brief_ids = (*CHAT_SYSTEM_TEMPLATE_IDS, 'chat.length.brief')
        long_ids = (*CHAT_SYSTEM_TEMPLATE_IDS, 'chat.length.long')
        proactive_ids = (*CHAT_SYSTEM_TEMPLATE_IDS, 'chat.proactive')
        before_brief_hash = prompt_metadata(
            'chat.system',
            brief_ids,
        )['promptHash']
        before_long_hash = prompt_metadata(
            'chat.system',
            long_ids,
        )['promptHash']
        before_proactive_hash = prompt_metadata(
            'chat.proactive',
            proactive_ids,
        )['promptHash']

        updated = update_prompt(
            'chat.length.long',
            '# 这一轮的篇幅\n验收版完整回应。',
        )
        after_brief_hash = prompt_metadata(
            'chat.system',
            brief_ids,
        )['promptHash']
        after_long_hash = prompt_metadata(
            'chat.system',
            long_ids,
        )['promptHash']
        after_proactive_hash = prompt_metadata(
            'chat.proactive',
            proactive_ids,
        )['promptHash']
        history = prompt_history('chat.length.long')

        assert after_brief_hash == before_brief_hash
        assert after_long_hash != before_long_hash
        assert after_proactive_hash == before_proactive_hash
        assert updated['fixed'] is False
        assert updated['placeholders'] == []
        assert history[0]['content'] == '# 这一轮的篇幅\n验收版完整回应。'
    finally:
        reset_prompts_for_tests()


def test_length_variants_are_listed_and_hot_editable(tmp_path) -> None:
    configure_prompts(tmp_path)
    try:
        items = {
            item['id']: item
            for item in list_prompts()
            if item['id'].startswith('chat.length.')
        }

        assert set(items) == {'chat.length.brief', 'chat.length.long'}
        assert all(item['fixed'] is False for item in items.values())
        assert all(item['placeholders'] == [] for item in items.values())
        update_prompt('chat.length.long', '# 这一轮的篇幅\n验收版完整回应。')
    finally:
        reset_prompts_for_tests()


def test_blank_prompt_is_rejected_before_write(tmp_path) -> None:
    configure_prompts(tmp_path)
    try:
        with pytest.raises(ValueError, match='不能为空'):
            update_prompt('chat.length.brief', ' \n\t')
    finally:
        reset_prompts_for_tests()


def test_chat_system_rejects_two_variants_for_same_placeholder() -> None:
    system_values = {
        placeholder: ''
        for placeholder in (
            'name', 'identity', 'relationship', 'time_context', 'birthday_note',
            'resumption', 'persona', 'activity', 'facts', 'episodes', 'reply_style',
            'tone', 'expression_habits',
        )
    }
    component_values = {template_id: {} for template_id in CHAT_SYSTEM_COMPONENTS}
    component_values['chat.protocol'] = {
        'emotions': 'normal',
        'gestures': 'heart',
        'emoji_rule': '本轮不支持发送表情包，不要写 <emoji> 标签。',
    }
    component_values['chat.length.brief'] = {}
    component_values['chat.length.long'] = {}

    with pytest.raises(ValueError, match='同时选择'):
        render_chat_system(system_values, component_values)


async def test_proactive_replay_does_not_require_length_variant(db) -> None:
    from src.core.observe.store import event_store

    render_params: dict[str, dict[str, str]] = {}
    base_prompt = build_system_prompt(
        name='月璃',
        birthday='',
        personality='测试人格',
        reply_style='自然回复',
        reply_length=None,
        render_params=render_params,
    )
    render_params['chat.proactive'] = {'situation': '测试主动场景'}
    prompt = f'{base_prompt}\n\n{get_prompt("chat.proactive").render(situation="测试主动场景")}'
    request = event_store.append('llm_request', 'generating', 1, 1, {
        'messages': [{'role': 'system', 'content': prompt}],
        'promptId': 'chat.proactive',
        'promptHash': prompt_metadata(
            'chat.proactive',
            CHAT_PROACTIVE_TEMPLATE_IDS,
        )['promptHash'],
        'renderParams': render_params,
    })
    provider = _CaptureReplayProvider()

    await replay_event(event_store, provider, request['seq'])

    assert '# 这一轮的篇幅' not in provider.messages[0]['content']


async def test_replay_uses_current_length_variant_text(db, tmp_path) -> None:
    from src.core.observe.store import event_store

    configure_prompts(tmp_path)
    try:
        render_params: dict[str, dict[str, str]] = {}
        original_prompt = build_system_prompt(
            name='月璃',
            birthday='',
            personality='测试人格',
            reply_style='自然回复',
            reply_length='brief',
            render_params=render_params,
        )
        request = event_store.append('llm_request', 'generating', 1, 1, {
            'messages': [{'role': 'system', 'content': original_prompt}],
            'promptId': 'chat.system',
            'promptHash': '旧模板哈希',
            'renderParams': render_params,
        })
        updated_text = '# 这一轮的篇幅\n验收修改后的简短篇幅文案。'
        update_prompt('chat.length.brief', updated_text)
        provider = _CaptureReplayProvider()

        await replay_event(event_store, provider, request['seq'])

        assert updated_text in provider.messages[0]['content']
    finally:
        reset_prompts_for_tests()


async def test_replay_rejects_event_before_current_prompt_structure(db) -> None:
    from src.core.observe.store import event_store

    provider = _RejectReplayProvider()
    request = event_store.append('llm_request', 'generating', 1, 1, {
        'messages': [{'role': 'system', 'content': '旧系统提示词'}],
        'promptId': 'chat.system',
        'promptHash': '旧模板哈希',
        'renderParams': {
            'chat.boundaries': {},
            'chat.discipline': {},
            'chat.protocol': {'emotions': 'normal', 'gestures': 'heart'},
            'chat.system': {
                'name': '测试角色',
                'identity': '测试身份',
                'relationship': '',
                'time_context': '测试时间',
                'birthday_note': '',
                'resumption': '',
                'persona': '',
                'activity': '',
                'facts': '',
                'episodes': '',
                'reply_style': '自然回复',
                'tone': '',
                'expression_habits': '',
                'discipline': '旧纪律',
                'boundaries': '旧边界',
                'protocol': '旧协议',
            },
        },
    })

    with pytest.raises(ValueError, match='事件早于当前模板结构'):
        await replay_event(event_store, provider, request['seq'])

    assert provider.calls == 0
    assert event_store.search(kinds=['replay_request']).events == []


def test_replay_placeholder_error_keeps_category_and_missing_name() -> None:
    from src.core.services.dev.replay import _rebuild_messages

    render_params: dict[str, dict[str, str]] = {}
    prompt = build_system_prompt(
        name='月璃',
        birthday='',
        personality='测试人格',
        reply_style='自然回复',
        reply_length='brief',
        render_params=render_params,
    )
    del render_params['chat.system']['name']

    with pytest.raises(ValueError) as caught:
        _rebuild_messages({
            'seq': 99,
            'messages': [{'role': 'system', 'content': prompt}],
            'promptId': 'chat.system',
            'renderParams': render_params,
        })

    message = str(caught.value)
    assert '事件早于当前模板结构，无法准确重放' in message
    assert '缺少 name' in message
