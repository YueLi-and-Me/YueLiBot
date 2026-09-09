"""验证 QQ 适配器事件分类和入站消息解析的纯函数契约。

本模块覆盖动作响应、心跳、普通事件及异常事件的分类结果，
确保解析器不会把协议响应误当成用户消息。
"""

from __future__ import annotations

import pytest

from src.platforms.onebot11.events import (
    build_poke_inbound_event,
    classify_event,
    is_action_response,
    is_heartbeat,
    parse_inbound_event,
)
from src.platforms.onebot11.config import GroupAccessConfig, PrivateAccessConfig
from src.platforms.onebot11.segments import (
    base64_image_segment,
    file_image_segment,
    image_source_urls,
    is_emoji_image,
    message_to_text,
)


def _private(message: list[dict], **overrides: object) -> dict:
    payload: dict = {
        'post_type': 'message',
        'message_type': 'private',
        'sub_type': 'friend',
        'message_id': 101,
        'user_id': 24680,
        'self_id': 13579,
        'message': message,
        'sender': {'user_id': 24680, 'nickname': '主人'},
    }
    payload.update(overrides)
    return payload


def _group(message: list[dict], **overrides: object) -> dict:
    payload = _private(message, message_type='private', user_id=97531)
    payload.update({'group_id': 86420, 'sender': {'user_id': 97531, 'nickname': '群友'}})
    payload.update(overrides)
    return payload


def test_action_response_requires_non_empty_string_echo() -> None:
    assert is_action_response({'echo': 'abc'})
    assert not is_action_response({'echo': ''})
    assert not is_action_response({'echo': None})
    assert not is_action_response({'echo': 123})


def test_event_filters_heartbeat_request_self_and_non_owner() -> None:
    assert is_heartbeat({'post_type': 'meta_event', 'meta_event_type': 'heartbeat'})
    assert classify_event(
        {'post_type': 'request', 'request_type': 'friend'},
        '13579',
        '24680',
        PrivateAccessConfig(),
        GroupAccessConfig(),
    ) == 'request'
    assert classify_event(
        _private(
            [{'type': 'text', 'data': {'text': '自己'}}],
            user_id=13579,
            sender={'user_id': 13579, 'nickname': '月璃'},
        ),
        '13579',
        '24680',
        PrivateAccessConfig(),
        GroupAccessConfig(),
    ) == 'self_message'
    assert classify_event(
        _private(
            [{'type': 'text', 'data': {'text': '陌生人'}}],
            user_id=11111,
            sender={'user_id': 11111, 'nickname': '陌生人'},
        ),
        '13579',
        '24680',
        PrivateAccessConfig(),
        GroupAccessConfig(),
    ) == 'private_denied'


def _poke(**overrides: object) -> dict:
    """构造一条群聊戳一戳通知，默认被戳者是登录账号。"""
    payload = {
        'post_type': 'notice',
        'notice_type': 'notify',
        'sub_type': 'poke',
        'group_id': 86420,
        'user_id': 11111,
        'target_id': 13579,
    }
    payload.update(overrides)
    return payload


def test_poke_at_self_is_classified_and_respects_access_lists() -> None:
    """戳 Bot 的戳一戳单独成类，且同样受群白名单与私聊名单约束。"""
    # 群准入只有白名单一种模式，空名单等于全拒；用例显式放行测试群号。
    open_groups = GroupAccessConfig(list=['86420'])
    assert classify_event(
        _poke(), '13579', '24680', PrivateAccessConfig(), open_groups,
    ) == 'poke'

    # 群名单外不因为一次戳就绕过准入。
    closed_groups = GroupAccessConfig(mode='whitelist', list=['99999'])
    assert classify_event(
        _poke(), '13579', '24680', PrivateAccessConfig(), closed_groups,
    ) == 'group_denied'

    # 私聊戳一戳没有 group_id，走私聊名单。
    private_poke = _poke(group_id=None)
    private_poke.pop('group_id')
    assert classify_event(
        private_poke,
        '13579',
        '24680',
        PrivateAccessConfig(mode='whitelist', list=['99999']),
        open_groups,
    ) == 'private_denied'


def test_poke_inbound_event_carries_names_and_no_platform_id() -> None:
    """戳一戳合成正文、置 poked_me，并留空平台编号（notice 通道本来就没有）。"""
    event = build_poke_inbound_event(_poke(), '月璃', '玖璃', '小玖')

    assert event.stream_kind == 'group'
    assert event.stream_external_id == '86420'
    assert event.sender_external_id == '11111'
    # 昵称与名片分开提交：空名片在主体侧等于「清除名片」，不能用昵称顶替。
    assert event.sender_nickname == '玖璃'
    assert event.sender_group_card == '小玖'
    assert event.text == '[戳了戳月璃]'
    assert event.external_message_id == ''
    assert event.poked_me is True
    assert event.mentioned_me is False


def test_poke_inbound_event_uses_protocol_action_wording() -> None:
    """协议端带了展示片段时按它的措辞渲染，缺失时退回默认说法。"""
    payload = _poke(raw_info=[
        {'type': 'qq', 'src': 'user'},
        {'type': 'nor', 'txt': '拍了拍'},
    ])

    assert build_poke_inbound_event(payload, '月璃', '玖璃', '').text == '[拍了拍月璃]'


def test_poke_inbound_event_rejects_empty_nickname() -> None:
    """昵称为空必须拒绝：空值提交会把发起者已存的群名片抹掉。"""
    with pytest.raises(ValueError, match='昵称不能为空'):
        build_poke_inbound_event(_poke(), '月璃', '   ', '小玖')


def test_poke_at_someone_else_is_not_ours() -> None:
    """群里两个人互戳、以及 Bot 自己戳出去的回显，都不归入 poke。"""
    # 群准入只有白名单一种模式，空名单等于全拒；用例显式放行测试群号。
    open_groups = GroupAccessConfig(list=['86420'])
    assert classify_event(
        _poke(target_id=22222), '13579', '24680', PrivateAccessConfig(), open_groups,
    ) == 'other'
    assert classify_event(
        _poke(user_id=13579, target_id=22222),
        '13579',
        '24680',
        PrivateAccessConfig(),
        open_groups,
    ) == 'other'


def test_private_access_whitelist_and_blacklist_both_exempt_owner() -> None:
    allowed = _private(
        [{'type': 'text', 'data': {'text': '名单内'}}],
        user_id=11111,
        sender={'user_id': 11111, 'nickname': '名单用户'},
    )
    denied = _private(
        [{'type': 'text', 'data': {'text': '名单外'}}],
        user_id=22222,
        sender={'user_id': 22222, 'nickname': '名单外用户'},
    )
    owner = _private([{'type': 'text', 'data': {'text': '用户本人'}}])

    whitelist = PrivateAccessConfig(mode='whitelist', list=['11111'])
    group_access = GroupAccessConfig()
    assert classify_event(allowed, '13579', '24680', whitelist, group_access) == 'message'
    assert classify_event(denied, '13579', '24680', whitelist, group_access) == 'private_denied'
    assert classify_event(owner, '13579', '24680', whitelist, group_access) == 'message'

    blacklist = PrivateAccessConfig(mode='blacklist', list=['11111', '24680'])
    assert classify_event(allowed, '13579', '24680', blacklist, group_access) == 'private_denied'
    assert classify_event(denied, '13579', '24680', blacklist, group_access) == 'message'
    assert classify_event(owner, '13579', '24680', blacklist, group_access) == 'message'


def test_private_access_rejects_invalid_mode_and_non_numeric_list() -> None:
    with pytest.raises(ValueError):
        PrivateAccessConfig(mode='allow_all', list=[])
    with pytest.raises(ValueError, match='数字 QQ 号列表'):
        PrivateAccessConfig(mode='whitelist', list=['not-a-qq'])


def test_private_owner_and_group_use_expected_external_ids() -> None:
    private = parse_inbound_event(
        _private([
            {'type': 'text', 'data': {'text': '你好'}},
            {'type': 'at', 'data': {'qq': '13579'}},
        ]),
        '13579',
        '月璃',
        '24680',
        PrivateAccessConfig(),
        GroupAccessConfig(),
    )
    assert private is not None
    assert private.stream_kind == 'direct'
    assert private.stream_external_id == '24680'
    assert private.mentioned_me is True

    group = parse_inbound_event(
        _group([{'type': 'text', 'data': {'text': '群里'}}]),
        '13579',
        '月璃',
        '24680',
        PrivateAccessConfig(),
        GroupAccessConfig(list=['86420']),
    )
    assert group is not None
    assert group.stream_kind == 'group'
    assert group.stream_external_id == '86420'
    assert group.sender_external_id == '97531'


def test_group_id_is_the_branch判据_even_when_message_type_is_private() -> None:
    event = parse_inbound_event(
        _group([{'type': 'text', 'data': {'text': '消息'}}]),
        '13579',
        '月璃',
        '24680',
        PrivateAccessConfig(),
        GroupAccessConfig(list=['86420']),
    )
    assert event is not None
    assert event.stream_kind == 'group'


def test_non_text_segments_are_preserved_as_placeholders() -> None:
    segments = [
        {'type': 'text', 'data': {'text': '看这个'}},
        {'type': 'image', 'data': {'sub_type': None}},
        {'type': 'image', 'data': {'sub_type': 0}},
        {'type': 'image', 'data': {'sub_type': 1}},
        {'type': 'record', 'data': {'file': 'a.silk'}},
    ]
    assert is_emoji_image(segments[1]) is False
    assert is_emoji_image(segments[2]) is False
    assert is_emoji_image(segments[3]) is True
    assert message_to_text(segments) == '看这个[图片][图片][表情包][语音]'


def test_image_source_urls_keep_placeholder_order_and_skip_emoji() -> None:
    segments = [
        {'type': 'image', 'data': {'sub_type': 0, 'url': 'https://img/a.png'}},
        {'type': 'image', 'data': {'sub_type': None, 'file': 'file://C:/b.png'}},
        {'type': 'image', 'data': {'sub_type': 1}},
        {'type': 'image', 'data': {'sub_type': 0}},
    ]

    assert image_source_urls(segments) == (
        'https://img/a.png',
        'file://C:/b.png',
        '',
    )


def test_parse_inbound_event_collects_ordinary_image_sources() -> None:
    event = parse_inbound_event(
        _group([
            {'type': 'text', 'data': {'text': '看这张'}},
            {'type': 'image', 'data': {'sub_type': 0, 'url': 'https://img/a.png'}},
            {'type': 'image', 'data': {'sub_type': 1, 'url': 'https://img/emoji.png'}},
        ]),
        '13579',
        '月璃',
        '24680',
        PrivateAccessConfig(),
        GroupAccessConfig(list=['86420']),
    )

    assert event is not None
    assert event.image_sources == ('https://img/a.png',)
    assert event.text == '看这张[图片][表情包]'


def test_outgoing_images_have_protocol_prefixes() -> None:
    # 底层工具只组装来源，不读取文件；出站字节策略由更上层的构造器负责。
    assert base64_image_segment('AAA') == {'type': 'image', 'data': {'file': 'base64://AAA'}}
    assert base64_image_segment('base64://BBB')['data']['file'] == 'base64://BBB'
    assert file_image_segment('C:/a.png') == {'type': 'image', 'data': {'file': 'file://C:/a.png'}}
    assert file_image_segment('file://D:/b.png')['data']['file'] == 'file://D:/b.png'
    assert file_image_segment('C:/a.png', sub_type=7)['data'] == {
        'file': 'file://C:/a.png', 'sub_type': 7,
    }
    with pytest.raises(ValueError, match='不能使用 file://'):
        base64_image_segment('file://a.png')
    with pytest.raises(ValueError, match='不能使用 base64://'):
        file_image_segment('base64://AAA')
    for constructor in (base64_image_segment, file_image_segment):
        with pytest.raises(ValueError, match='不能为空'):
            constructor(' ')
