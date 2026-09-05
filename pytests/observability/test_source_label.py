"""消息来源标签的纯函数契约。"""

from src.core.observe.source import source_label
from src.core.platform_io.types import StreamRef


def test_source_label_distinguishes_all_stream_kinds() -> None:
    """三个 stream kind 只靠传入行即可生成互不混淆的标签。"""

    desktop = StreamRef(id=1, platform='desktop', kind='desktop', external_id='desktop')
    direct = StreamRef(id=2, platform='qq', kind='direct', external_id='10001')
    group = StreamRef(id=3, platform='qq', kind='group', external_id='629201002')
    named_group = StreamRef(
        id=4,
        platform='qq',
        kind='group',
        external_id='629201003',
        display_name='月璃的小窝',
    )

    assert source_label(desktop) == '桌面'
    assert source_label(direct, direct_name='联系人甲') == '私聊·联系人甲'
    assert source_label(direct) == '私聊·10001'
    assert source_label(group) == '群聊·629201002'
    assert source_label(named_group) == '群聊·月璃的小窝'
    assert len({
        source_label(desktop),
        source_label(direct, direct_name='联系人甲'),
        source_label(group),
    }) == 3
