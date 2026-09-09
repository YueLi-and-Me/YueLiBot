"""共处群注入（K3）验收。

非群聊会话注入「你和对方同在这些群」一行，回答「你知道我常在哪个群吗」；
群聊会话整块省略——它描述的是在场者，当众复述等于暴露别的群的存在。
两个渲染器（传统角色模式与工具模式）共用同一份输入，取数只在组装期一次。
"""

from __future__ import annotations

import sqlite3

from src.core.agent.prompt import build_itemized_system_prompt, build_system_prompt
from src.core.config.schema import Config
from src.core.services.chat import ChatService

NOW = 1_800_000_000_000
GROUP_NAME = '♡小水のPaPa♡'
GROUP_EXTERNAL_ID = '629201002'
UNNAMED_GROUP_EXTERNAL_ID = '958605377'
EXPECTED_LINE = f'你和凌白同在这些群：{GROUP_NAME}、{UNNAMED_GROUP_EXTERNAL_ID}'


def _build_kwargs(shared_groups) -> dict:
    return dict(
        name='月璃',
        birthday='',
        personality='测试人格',
        reply_style='自然回复',
        shared_groups=shared_groups,
    )


def test_traditional_renderer_emits_shared_groups_line() -> None:
    """★K3-3 / ★K3-2：传统渲染器输出一行，群名为空的那个显示群号。"""

    prompt = build_system_prompt(**_build_kwargs(('凌白', (GROUP_NAME, UNNAMED_GROUP_EXTERNAL_ID))))

    assert EXPECTED_LINE in prompt


def test_itemized_renderer_emits_shared_groups_line() -> None:
    """★K3-3 / ★K3-2：工具模式渲染器对同一份输入产出同一行。"""

    _system, items = build_itemized_system_prompt(
        **_build_kwargs(('凌白', (GROUP_NAME, UNNAMED_GROUP_EXTERNAL_ID))),
    )

    assert any('[共处群聊]' in item and EXPECTED_LINE in item for item in items)


def test_both_renderers_omit_block_without_shared_groups() -> None:
    """无共处群时整块省略，不输出只有标题的空块。"""

    for value in (None, ('凌白', ())):
        prompt = build_system_prompt(**_build_kwargs(value))
        _system, items = build_itemized_system_prompt(**_build_kwargs(value))
        assert '同在这些群' not in prompt
        assert all('共处群聊' not in item for item in items)


def test_omitted_shared_groups_is_byte_identical_to_default() -> None:
    """★K3-1 的渲染层半证：显式传 None 与不传逐字节相同（群聊拿到的恒为 None）。"""

    for builder in (build_system_prompt, build_itemized_system_prompt):
        base_kwargs = _build_kwargs(None)
        explicit = builder(**base_kwargs)
        default_kwargs = {key: value for key, value in base_kwargs.items() if key != 'shared_groups'}
        assert explicit == builder(**default_kwargs)


def _make_chat(db: sqlite3.Connection) -> ChatService:
    async def _noop(_channel: str, _payload: object, _stream_id: int = 1) -> None:
        return None

    return ChatService(
        db=db,
        chat_provider=None,
        proactive_provider=None,
        summary_provider=None,
        push_event=_noop,
        cfg=Config(),
    )


def _contact_with_two_groups(chat: ChatService):
    """构造一个与 Bot 共处两群的人物：一个群有群名，另一个群名为空。"""

    registry = chat._registry
    registry.resolve_inbound(
        platform='qq', stream_kind='group', stream_external_id=GROUP_EXTERNAL_ID,
        sender_external_id='3209184542', sender_nickname='凌白',
        sender_group_card='凌白', first_seen_at=NOW,
    )
    registry.set_group_display_name('qq', GROUP_EXTERNAL_ID, GROUP_NAME)
    registry.resolve_inbound(
        platform='qq', stream_kind='group', stream_external_id=UNNAMED_GROUP_EXTERNAL_ID,
        sender_external_id='3209184542', sender_nickname='凌白',
        sender_group_card='凌白', first_seen_at=NOW,
    )
    return registry.resolve_inbound(
        platform='qq', stream_kind='direct', stream_external_id='3209184542',
        sender_external_id='3209184542', sender_nickname='凌白',
        sender_group_card='', first_seen_at=NOW,
    )


def test_direct_turn_collects_shared_groups_with_group_number_fallback(
    db: sqlite3.Connection,
) -> None:
    """私聊组装出「对方显示名 + 两个共处群」，群名为空的退回群号。"""

    chat = _make_chat(db)
    context = _contact_with_two_groups(chat)

    prepared = chat._prepare_turn_context(context, '你知道我在哪个群吗', NOW)

    assert prepared.shared_groups == ('凌白', (GROUP_NAME, UNNAMED_GROUP_EXTERNAL_ID))
    messages = chat._render_prepared_context(prepared)
    assert EXPECTED_LINE in messages[0]['content']


def test_direct_turn_collects_shared_groups_in_itemized_mode(
    db: sqlite3.Connection,
) -> None:
    """工具模式同样注入：真机跑的是 item 流，只接传统渲染器等于没做。"""

    chat = _make_chat(db)
    chat._tool_calling = True
    context = _contact_with_two_groups(chat)
    prepared = chat._prepare_turn_context(context, '你知道我在哪个群吗', NOW)

    messages = chat._render_prepared_context(prepared, protocol_text='协议占位')

    assert any(
        '[共处群聊]' in message['content'] and EXPECTED_LINE in message['content']
        for message in messages
        if message['role'] == 'user'
    )


def test_group_turn_never_collects_shared_groups(db: sqlite3.Connection) -> None:
    """★K3-1 的取数层半证：群聊会话的组装结果恒为 None，渲染层整块省略。"""

    chat = _make_chat(db)
    _contact_with_two_groups(chat)
    group_context = chat._registry.resolve_inbound(
        platform='qq', stream_kind='group', stream_external_id=GROUP_EXTERNAL_ID,
        sender_external_id='3209184542', sender_nickname='凌白',
        sender_group_card='凌白', first_seen_at=NOW + 1,
    )

    prepared = chat._prepare_turn_context(group_context, '你知道我在哪个群吗', NOW)

    assert prepared.shared_groups is None
    messages = chat._render_prepared_context(prepared)
    assert '同在这些群' not in messages[0]['content']


def test_contact_without_any_group_gets_no_block(db: sqlite3.Connection) -> None:
    """对方没有任何群成员关系时，整块省略。"""

    chat = _make_chat(db)
    context = chat._registry.resolve_inbound(
        platform='qq', stream_kind='direct', stream_external_id='7758',
        sender_external_id='7758', sender_nickname='路人',
        sender_group_card='', first_seen_at=NOW,
    )

    prepared = chat._prepare_turn_context(context, '你好', NOW)

    assert prepared.shared_groups is None
