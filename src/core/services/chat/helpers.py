"""对话编排用到的模块级纯函数。

本模块收拢不依赖 ``ChatService`` 实例的转换逻辑：召回事实到提示词条目的组装
（含同槽冲突的整组补齐）、模型完整响应到分句字典的提取、解析事件到出站分句的
收集、表情命中到助手历史标签的序列化，以及日程对象到前端字典的转换。

调用方是 ``src.core.services.chat.service``；``_facts_for_prompt`` 另有测试直接引用。
"""

from __future__ import annotations

from html import escape
from typing import Sequence

from src.core.agent.parser import (
    ParseEvent,
    ResponseParser,
    SayEndEvent,
    SayEvent,
    TextEvent,
)
from src.core.agent.prompt import MemoryFactItem
from src.core.agent.segmentation import split_into_bubbles
from src.core.config.schema import TypingConfig
from src.core.memory.store import MemoryStore, RecalledFact
from src.core.schedule.plan import DayPlan


def _facts_for_prompt(
    memory: MemoryStore,
    person_id: int,
    facts: Sequence[RecalledFact],
    *,
    exclude_marked: bool = False,
) -> list[MemoryFactItem]:
    """把召回事实组装成提示词条目，同槽冲突的整组标注并补齐缺失成员。

    冲突事实只注入一半等于没注入：模型只看到一边就会把那边当成定论。
    因此同槽冲突组的全体成员（包括本轮没被召回的）都进入提示词并排呈现，
    不按时间取新、不按分数取高。

    「已被纠正」的事实由读取入口在截断之前排除，``facts`` 里不应含有；这里只
    负责冲突组补齐的那部分成员，它们不经读取入口，直接按槽位从库里取。

    :param memory: 记忆存储实例。
    :param person_id: 事实所属人物 ID。
    :param facts: 本轮已选中的召回事实。
    :param exclude_marked: 为真时带「已被纠正」标记的事实不参与冲突分组，
        也不作为同组成员补齐；取值与读取入口的同名参数一致。
    :return: 供 ``build_system_prompt`` 渲染的事实条目列表。
    副作用：只读 facts 与 memory_feedback_results 表。
    """

    if not facts:
        return []
    groups = memory.slot_conflicts(
        person_id, [fact.id for fact in facts], exclude_marked=exclude_marked,
    )
    items = [
        MemoryFactItem(
            content=fact.content,
            slot=groups[fact.id][0] if fact.id in groups else '',
            conflicting=fact.id in groups,
            fact_id=fact.id,
        )
        for fact in facts
    ]
    # 同组里本轮没被选中的成员一并补上：并排呈现的前提是两边都在场。
    selected = {fact.id for fact in facts}
    appended: set[int] = set()
    for slot, members in groups.values():
        for member_id, content in members:
            if member_id not in selected and member_id not in appended:
                appended.add(member_id)
                items.append(MemoryFactItem(content=content, slot=slot, conflicting=True, fact_id=member_id))
    return items


def _extract_lines(raw: str) -> list[dict] | None:
    """将带协议标签的完整模型响应提取为分句字典。

    :param raw: 模型返回的完整文本。

    :return: 每个 ``<say>`` 分句的 ``text`` 和可选 ``emotion`` 字典；没有完整正文时
        返回 ``None``。
    """

    parser = ResponseParser()
    lines: list[dict] = []
    cur: dict | None = None
    for e in [*parser.push(raw), *parser.flush()]:
        if isinstance(e, SayEvent):
            cur = {'text': '', **({'emotion': e.emotion} if e.emotion else {})}
            if e.delivery == 'voice':
                cur['delivery'] = 'voice'
        elif isinstance(e, TextEvent) and cur is not None:
            cur['text'] += e.value
        elif isinstance(e, SayEndEvent) and cur is not None:
            if cur['text'].strip():
                lines.append({**cur, 'text': cur['text'].strip()})
            cur = None
    return lines if lines else None


def _collect_outbound_segment(
    event: ParseEvent,
    segments: list[str],
    current: list[str] | None,
    typing: TypingConfig,
    *,
    voice: bool = False,
) -> list[str] | None:
    """按解析器识别的 ``say`` 边界收集外部平台正文。

    :param event: 当前解析事件。
    :param segments: 已完成的分句列表，会被原地追加。
    :param current: 当前尚未结束的分句片段列表。
    :param typing: 打字节奏配置，决定一条台词切成几条气泡。
    :param voice: 语音保留完整台词和标点，不做文字气泡切分。

    :return: 更新后的当前分句片段；收到 ``SayEndEvent`` 后返回 ``None``。

    副作用：
        可能向 ``segments`` 原地追加一条或多条非空分句，不重新扫描完整响应文本。
        一个 ``<say>`` 按打字习惯再切成气泡，因此追加条数可能多于 ``<say>`` 数。
    """
    if isinstance(event, SayEvent):
        return []
    if isinstance(event, TextEvent):
        if current is None:
            current = []
        current.append(event.value)
        return current
    if isinstance(event, SayEndEvent):
        if current is not None:
            # 在这里切分而不是在投递侧：平台出站、助手历史和控制台渲染共用这份
            # segments，切分前置才能保证三者看到的气泡完全一致。
            text = ''.join(current).strip()
            if voice:
                if text:
                    segments.append(text)
            else:
                segments.extend(split_into_bubbles(text, typing))
        return None
    return current


def _emoji_history_markup(items: list[tuple[str, str, int]]) -> str:
    """把实际命中的目标情绪序列化为可统计的助手历史标签。"""

    return ''.join(
        f'<emoji emotion="{escape(emotion, quote=True)}"/>'
        for emotion, _reference, _sub_type in items
    )


def _plan_to_dict(plan: DayPlan | None) -> dict | None:
    """将可选日程对象转换为前端使用的字典。

    :param plan: 待转换日程；可以为 ``None``。

    :return: JSON 兼容日程字典；输入为 ``None`` 时返回 ``None``。
    """

    if plan is None:
        return None
    return {
        'date': plan.date,
        'theme': plan.theme,
        'intentions': [
            {
                'what': intention.what,
                'carriedDays': intention.carried_days,
            }
            for intention in plan.intentions
        ],
        'roughRhythm': plan.rough_rhythm,
    }
