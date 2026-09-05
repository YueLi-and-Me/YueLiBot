"""事实块渲染：同槽冲突并排呈现并标注（★L-2）。"""

from __future__ import annotations

from datetime import datetime

from src.core.agent.prompt import (
    MemoryFactItem,
    build_itemized_system_prompt,
    build_system_prompt,
)

TEST_NAME = '测试角色'
TEST_PERSONALITY = '喜欢观察细节，说话直接。'
TEST_REPLY_STYLE = '像熟人私聊，默认简短接话。'


def _facts_prompt(facts: list) -> str:
    return build_system_prompt(
        name=TEST_NAME,
        birthday='',
        personality=TEST_PERSONALITY,
        reply_style=TEST_REPLY_STYLE,
        schedule='',
        now=datetime(2032, 7, 15, 12, 5),
        facts=facts,
    )


def test_conflicting_facts_render_side_by_side_with_annotation() -> None:
    """同一槽位对不上的多条并排出现并明确标注——不是二选一，也不是只给新的。"""

    prompt = _facts_prompt([
        MemoryFactItem(content='他现在住在成都', slot='居住地', conflicting=True),
        MemoryFactItem(content='他习惯深夜写代码'),
        MemoryFactItem(content='他现在住在杭州', slot='居住地', conflicting=True),
    ])

    assert '关于「居住地」' in prompt
    assert '对不上' in prompt
    # 两条都出现，且都被收进同一个分组块里（成员行缩进在分组标题之后）。
    group_at = prompt.index('关于「居住地」')
    chengdu_at = prompt.index('他现在住在成都')
    hangzhou_at = prompt.index('他现在住在杭州')
    assert group_at < chengdu_at < hangzhou_at
    # 无冲突的事实仍是普通条目。
    assert '- 他习惯深夜写代码' in prompt


def test_conflicting_group_members_render_adjacent() -> None:
    """同组成员被拉到一起渲染，即便输入顺序中间隔着别的条目。"""

    prompt = _facts_prompt([
        MemoryFactItem(content='他现在住在成都', slot='居住地', conflicting=True),
        MemoryFactItem(content='他习惯深夜写代码'),
        MemoryFactItem(content='他现在住在杭州', slot='居住地', conflicting=True),
    ])

    facts_section = prompt[prompt.index('# 你早就知道的事'):]
    lines = [line for line in facts_section.splitlines() if line.strip()]
    group_at = lines.index('- 关于「居住地」，你先后记下了对不上的几条：')
    assert lines[group_at + 1] == '  - 他现在住在成都'
    assert lines[group_at + 2] == '  - 他现在住在杭州'


def test_plain_string_facts_still_render_as_plain_bullets() -> None:
    """无槽位的普通条目保持原有平铺形态，不引入分组标记。"""

    prompt = _facts_prompt(['他不吃香菜'])

    assert '- 他不吃香菜' in prompt
    assert '对不上' not in prompt


def test_single_slotted_fact_without_conflict_renders_as_plain_bullet() -> None:
    """带槽位但没有冲突的事实不分组：槽位本身不构成冲突。"""

    prompt = _facts_prompt([MemoryFactItem(content='他现在住在成都', slot='居住地')])

    assert '- 他现在住在成都' in prompt
    assert '对不上' not in prompt


def test_itemized_prompt_renders_the_same_conflict_group() -> None:
    """工具模式的独立上下文项里，冲突组同样并排呈现。"""

    _system, items = build_itemized_system_prompt(
        name=TEST_NAME,
        birthday='',
        personality=TEST_PERSONALITY,
        reply_style=TEST_REPLY_STYLE,
        now=datetime(2032, 7, 15, 12, 5),
        facts=[
            MemoryFactItem(content='他现在住在成都', slot='居住地', conflicting=True),
            MemoryFactItem(content='他现在住在杭州', slot='居住地', conflicting=True),
        ],
    )

    memory_item = next(item for item in items if '你早就知道的事' in item)
    assert '关于「居住地」' in memory_item
    assert '他现在住在成都' in memory_item
    assert '他现在住在杭州' in memory_item
