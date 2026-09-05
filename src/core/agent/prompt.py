"""组装主对话和主动搭话使用的系统提示词。

本模块把配置人格、时间、关系、记忆、活动、日程和表达习惯分别渲染为独立块，
再交给 `src.core.prompts.registry` 中的固定提示词资源组合；它只负责文本构造，不调用模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .action_protocol import COGNITIVE_ACTIONS
from .profile import InjectionProfile
from .vocab import EXPRESSION_IDS, GESTURE_IDS

from src.core.runtime.clock import now as current_time
from src.core.prompts.registry import (
    CHAT_PROTOCOL_TEMPLATE_ID,
    CHAT_SYSTEM_COMPONENTS,
    REPLY_LENGTH_TEMPLATE_IDS,
    get_prompt,
    render_chat_system,
)

# 重逢措辞使用有序阈值表统一维护，测试固定阈值顺序；第三档的天数由函数动态生成。
RESUMPTION_TIERS: List[Tuple[int, str]] = [
    (6 * 60 * 60_000, '距离你们上次说话过了几个小时。'),
    # 上界固定为 24 小时，确保“隔了一夜”只覆盖确实不超过一天的间隔。
    # 超过一天时转入动态天数描述，避免固定文案与实际间隔不一致。
    (24 * 60 * 60_000, '距离你们上次说话隔了一夜。'),
]


def _time_context(now: datetime, schedule: Optional[str] = None) -> str:
    """把本地时间和可选日程转换为低干扰的对话背景块。

    :param now: 用于显示日期、星期、时段和分钟的时间对象。
    :param schedule: 可选的当天日程文本，默认值为 `None`。
    :return: 中文时间背景；有日程时在空行后追加日程内容。
    副作用：不访问系统时钟，不修改输入对象。
    """
    hour = now.hour
    if hour < 5:
        period = '凌晨'
    elif hour < 9:
        period = '清晨'
    elif hour < 12:
        period = '上午'
    elif hour < 14:
        period = '中午'
    elif hour < 18:
        period = '下午'
    elif hour < 23:
        period = '晚上'
    else:
        period = '深夜'
    weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    lines = [
        f'现在是 {now.year}年{now.month}月{now.day}日{weekdays[now.weekday()]}，{period}{hour}点{now.minute:02d}分。',
        '时间只是这段对话的背景。除非对方正在聊作息、饭点或时间本身，否则不要主动提起时间；'
        '确有必要时提一句即可，不要每到固定时段重复问候。',
    ]
    if schedule:
        lines.extend(['', schedule])
    return '\n'.join(lines)


def _relationship_context(user_nickname: Optional[str], relationship: Optional[str]) -> str:
    """渲染称呼偏好和关系描述。

    :param user_nickname: 对方希望使用的称呼；`None` 或空字符串表示未配置。
    :param relationship: 对方在 Bot 视角下的关系文本；默认可为空。
    :return: 由一或两行关系规则组成的字符串，两个参数都为空时返回空字符串。
    副作用：不执行 I/O。
    """
    lines: List[str] = []
    if user_nickname:
        lines.append(f'对方希望你称呼「{user_nickname}」。只在自然需要称呼时用，不要每句话都带名字。')
    if relationship:
        lines.append(
            f'你把对方当{relationship}看待。这会影响你的分寸和亲近感，但不要反复声明这层关系，'
            '也不要在每句话里都带上称呼。'
        )
    return '\n'.join(lines)


def describe_resumption(gap_ms: int) -> str:
    """将上次对话至今的静默时长转换为不附带情绪判断的时间描述。

    :param gap_ms: 静默时长，单位为毫秒；非负值表示经过的实际时间，负值按最短档位处理。

    :return: 与时长对应的中文描述：少于 6 小时、6 至 24 小时或超过 24 小时三档。

    :raises TypeError: ``gap_ms`` 不支持与整数比较或整除时抛出。
    """
    for threshold, description in RESUMPTION_TIERS:
        if gap_ms < threshold:
            return description
    days = gap_ms // (24 * 60 * 60_000)
    return f'距离你们上次说话已经过去 {days} 天。'


def _prefixed_block(content: Optional[str]) -> str:
    """为非空动态上下文添加提示词段落分隔符。

    :param content: 可选提示词内容；``None`` 或空字符串表示不生成段落。

    :return: 非空内容前追加两个换行符的字符串；空内容返回空字符串。
    """

    return f'\n\n{content}' if content else ''


def _identity_context(
    personality: str,
    birthday: str,
    now: datetime,
    aliases: Optional[List[str]],
    platform_name: Optional[str],
    name: str,
) -> Tuple[str, date | None]:
    """从人格配置、生日、别名和当前时间构造身份提示词块。

    :param personality: 已配置的人格描述文本。
    :param birthday: ISO ``YYYY-MM-DD`` 格式的生日文本；空字符串表示未配置。
    :param now: 用于写入生日日期、计算年龄和判断生日当天的当前本地时间。
    :param aliases: 可供识别的其他称呼；``None`` 表示未配置。
    :param platform_name: 平台侧显示的 Bot 名称；为空时不加入别名。
    :param name: 配置中的主名称，用于去除重复别名。

    :return: ``(身份提示词, 解析后的生日)``；未配置生日时第二项为 ``None``。

    :raises ValueError: ``birthday`` 不是合法 ISO 日期文本。
    :raises TypeError: 参数类型不支持日期、列表或字符串操作时抛出。
    """

    lines = [personality]
    parsed_birthday: date | None = None
    if birthday:
        parsed_birthday = date.fromisoformat(birthday)
        age = now.year - parsed_birthday.year - (
            (now.month, now.day) < (parsed_birthday.month, parsed_birthday.day)
        )
        # 只写年龄不足以回答“生日是哪天”：模型会自行编造月份和日期。
        # 必须把完整日期写入身份块，让生日成为模型可直接读取的稳定事实。
        lines.extend([
            '',
            f'你的生日是 {parsed_birthday.year}年{parsed_birthday.month}月{parsed_birthday.day}日。',
            f'你今年 {age} 岁。',
        ])

    self_names = [value for value in [*(aliases or []), platform_name] if value and value != name]
    if self_names:
        unique_names = list(dict.fromkeys(self_names))
        lines.extend([
            '',
            f'别人也可能用这些名字叫你：{"、".join(unique_names)}。这些都是你的称呼。',
        ])
    return '\n'.join(lines), parsed_birthday


def _relationship_block(
    acquaintance: Optional[str],
    user_nickname: Optional[str],
    relationship: Optional[str],
) -> str:
    """把熟悉程度、称呼和关系信息包装成可选提示词段落。

    :param acquaintance: 已计算出的熟悉程度描述；可为空。
    :param user_nickname: 对方称呼偏好；可为空。
    :param relationship: 关系文本；可为空。
    :return: 带段落前缀的关系块，所有输入为空时返回空字符串。
    副作用：不修改输入列表或文本。
    """
    lines: List[str] = []
    if acquaintance:
        lines.extend(['# 你们的关系走到哪里了', acquaintance])
    relationship_context = _relationship_context(user_nickname, relationship)
    if relationship_context:
        lines.append(relationship_context)
    return _prefixed_block('\n\n'.join(lines))


def _activity_block(activity: Optional[str]) -> str:
    """把当前前台活动包装为不要求主动提及的情境块。

    :param activity: 前台活动描述；默认可为 `None`。
    :return: 带情境说明和使用限制的提示词块；无活动时返回空字符串。
    副作用：不执行屏幕读取或其他 I/O。
    """
    if not activity:
        return ''
    return _prefixed_block('\n'.join([
        '# 眼前的一点情境',
        activity,
        '这只是后台得到的背景信息，不是监控报告。与当前话题无关时不要提及，'
        '也不要复述为「我看到你正在……」。',
    ]))


def _scene_block(scene: Optional[Tuple[str, str]]) -> str:
    """把场景画像包装成不要求主动提及的群聊背景块。

    与 ``_activity_block`` 同一纪律：后台取得的背景信息，不要求 Bot 复述。
    观察 Agent 只在群聊运行，私聊与桌面传 ``None``、整块省略。

    :param scene: ``(话题, 气氛)`` 二元组；``None`` 表示还没有观察结果。
    :return: 带段落前缀的提示词块，无场景时返回空字符串。
    """
    if scene is None:
        return ''
    topic, atmosphere = scene
    return _prefixed_block('\n'.join([
        '# 群里现在的情况',
        f'大家在聊：{topic}',
        f'气氛：{atmosphere}',
        '这是当前群聊的概览，用于判断本轮是否发言以及用什么语气。'
        '不要复述这些内容，也不要为了呼应气氛而强行发言。',
    ]))


def _jargon_block(jargon: Optional[Sequence[Tuple[str, str]]]) -> str:
    """把本轮命中的黑话渲染为背景类提示词块。

    与 ``_activity_block`` / ``_scene_block`` 同一纪律：只注入本轮消息里
    实际命中的词条（查表与截断在 ``agent/jargon.py``，此处为纯渲染器）。
    表头声明这是机械匹配的结果、可能不准确、仅用于理解消息内容，缺少该
    声明时模型会把词条释义当作可信事实转述；收尾注明仅供理解、不要刻意
    使用，缺少该约束时模型会把理解词义当成本轮任务。

    :param jargon: ``(词, 含义)`` 序列；`None` 或空序列表示不生成该块。
    :return: 带段落前缀的黑话块；无命中时返回空字符串，整块省略。
    """
    if not jargon:
        return ''
    return _prefixed_block('\n'.join([
        '# 这个群里的一些说法',
        '以下说法是按字面从上下文匹配的结果，可能不准确，仅用于理解消息内容。',
        *[f'「{term}」= {meaning}' for term, meaning in jargon],
        '',
        '这些是群内常用说法，能看懂即可。不要刻意使用，也不要向群成员解释词义。',
    ]))


def _impressions_block(impressions: Optional[Sequence['InjectionProfile']]) -> str:
    """把在场者的人物画像渲染为背景类提示词块，确凿档与印象档分开标注。

    与 ``_jargon_block`` 同一纪律：只注入本轮在场者的画像（取数与上限在
    ``agent/profile.py``，此处为纯渲染器）。两档必须能让读提示词的人
    （和模型自己）分清「这是记着的」和「这是印象」：确凿档逐条有账本
    记录可查，印象档只是模型收敛出的感觉。收尾注明仅供自身参考、不向
    对方复述，缺少该约束时模型会把印象陈述当成本轮任务。

    :param impressions: 画像条目序列；``None`` 或空序列表示整块省略，
        不输出只有标题的空块。
    :return: 带段落前缀的印象块；无画像时返回空字符串。
    """
    if not impressions:
        return ''
    sections: List[str] = []
    for item in impressions:
        lines: List[str] = []
        if item.confirmed:
            lines.append('记着的：')
            lines.extend(f'- {entry.label}：{entry.content}' for entry in item.confirmed)
        if item.impression:
            lines.append(f'印象：{item.impression}')
        if lines:
            sections.append('\n'.join(lines))
    if not sections:
        return ''
    return _prefixed_block('\n'.join([
        '# 你对他们的印象',
        '\n\n'.join(sections),
        '',
        '「记着的」逐条有记录可查；「印象」只是你的感觉，未必准确。'
        '这些都仅供自己参考，不要向对方复述，也不要作为对人的定论。',
    ]))


@dataclass(frozen=True)
class MemoryFactItem:
    """注入主对话提示词的一条长期事实。

    :ivar content: 事实正文。
    :ivar slot: 单值槽位名；空串表示多值事实，不参与冲突分组。
    :ivar conflicting: 同一槽位下是否还有其他活跃事实与之对不上；为真时
        本条目与同槽成员并排渲染并明确标注。
    :ivar fact_id: 事实行 ID；反馈纠错的锚点登记用，``0`` 表示来源不明的
        纯文本条目。
    """

    content: str
    slot: str = ''
    conflicting: bool = False
    fact_id: int = 0


def _memory_block(title: str, values: Optional[List[str]], instruction: str) -> str:
    """把一组记忆条目渲染为带标题和使用规则的列表块。

    :param title: 提示词中显示的区块标题。
    :param values: 记忆文本列表；`None` 或空列表表示不注入该块。
    :param instruction: 约束模型如何使用这些记忆的说明。
    :return: 带段落前缀的 Markdown 风格列表，记忆为空时返回空字符串。
    副作用：不修改传入列表。
    """
    if not values:
        return ''
    return _prefixed_block('\n'.join([
        f'# {title}',
        *[f'- {value}' for value in values],
        instruction,
    ]))


def _facts_block(
    title: str,
    facts: Optional[Sequence['str | MemoryFactItem']],
    instruction: str,
) -> str:
    """把长期事实渲染为带标题和使用规则的列表块，同槽冲突的多条并排呈现。

    冲突不做取舍：不按时间取新、不按分数取高、不替她二选一——对不上的几条
    全部保留并明确标注，把核实的余地留给她与当事人的对话。这是
    ``_memory_block`` 的事实专用形态；情节块不需要分组，继续走 ``_memory_block``。

    :param title: 提示词中显示的区块标题。
    :param facts: 事实条目；纯字符串按无槽位事实渲染。``None`` 或空序列表示
        整块省略，不输出只有标题的空块。
    :param instruction: 约束模型如何使用这些记忆的说明。
    :return: 带段落前缀的 Markdown 风格列表；无事实时返回空字符串。
    副作用：不修改传入序列。
    """

    if not facts:
        return ''
    items = [
        fact if isinstance(fact, MemoryFactItem) else MemoryFactItem(content=str(fact))
        for fact in facts
    ]
    groups: Dict[str, List[MemoryFactItem]] = {}
    for item in items:
        if item.slot and item.conflicting:
            groups.setdefault(item.slot, []).append(item)
    lines: List[str] = [f'# {title}']
    rendered_groups: set[str] = set()
    for item in items:
        if item.slot in groups:
            if item.slot in rendered_groups:
                continue
            rendered_groups.add(item.slot)
            lines.append(f'- 关于「{item.slot}」，你先后记下了对不上的几条：')
            lines.extend(f'  - {member.content}' for member in groups[item.slot])
        else:
            lines.append(f'- {item.content}')
    if rendered_groups:
        lines.append('对不上的几条都原样并列在上面了；不要自己选定哪条为准，合适的时候可以当面问。')
    lines.append(instruction)
    return _prefixed_block('\n'.join(lines))


def _expression_habits_block(expression_habits: Optional[str]) -> str:
    """把当前轮表达习惯放入独立提示词段落。

    :param expression_habits: 已渲染的表达习惯文本；默认可为 `None`。
    :return: 以“平时的说法”为标题的提示词块，无内容时返回空字符串。
    副作用：不执行 I/O。
    """
    if not expression_habits:
        return ''
    return _prefixed_block(f'# 表达方式参考\n{expression_habits}')


def build_system_prompt(
    name: str,
    birthday: str,
    personality: str,
    reply_style: str,
    now: Optional[datetime] = None,
    persona: Optional[str] = None,
    acquaintance: Optional[str] = None,
    facts: Optional[Sequence['str | MemoryFactItem']] = None,
    episodes: Optional[List[str]] = None,
    activity: Optional[str] = None,
    schedule: Optional[str] = None,
    user_nickname: Optional[str] = None,
    relationship: Optional[str] = None,
    expression_habits: Optional[str] = None,
    reply_length: Optional[str] = None,
    tone: Optional[str] = None,
    resumption: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    platform_name: Optional[str] = None,
    render_params: Optional[Dict[str, Dict[str, str]]] = None,
    protocol_text: Optional[str] = None,
    emoji_enabled: bool = False,
    emoji_tags: Sequence[str] = (),
    scene: Optional[Tuple[str, str]] = None,
    jargon: Optional[Sequence[Tuple[str, str]]] = None,
    impressions: Optional[Sequence[InjectionProfile]] = None,
    decision_only: bool = False,
) -> str:
    """组装主对话系统提示词，并将各类上下文注入对应的固定区块。

    :param name: Bot 的主名称。
    :param birthday: ISO ``YYYY-MM-DD`` 格式的生日文本；空字符串表示未配置。
    :param personality: 人格和身份描述文本。
    :param reply_style: 回复风格约束文本。
    :param now: 用于时间、年龄和生日判断的当前时间；省略时读取系统时钟。
    :param persona: 可选的额外人格上下文。
    :param acquaintance: 可选的熟悉程度描述。
    :param facts: 可选的长期事实记忆列表；条目为 ``MemoryFactItem`` 时，
        同一槽位下对不上的多条会并排渲染并明确标注。
    :param episodes: 可选的近期对话回想列表。
    :param activity: 可选的当前前台活动描述。
    :param schedule: 可选的当天日程文本。
    :param user_nickname: 对方偏好的称呼。
    :param relationship: 对方在 Bot 视角下的关系描述。
    :param expression_habits: 已渲染的表达习惯提示词块。
    :param reply_length: 当前轮规划出的回复篇幅枚举；非回复场景可为空。
    :param tone: 当前轮临时语调提示。
    :param resumption: 当前对话恢复提示。
    :param aliases: 可选的其他 Bot 名称列表。
    :param platform_name: 平台侧显示的 Bot 名称。
    :param protocol_text: 可选的整体输出协议文本；提供时直接替换 ``chat.protocol``
        在「输出格式」块中的位置，用于 Agent 模式把「先 <decision> 再 <say>」
        变成唯一主指令，而不是追加成与既有直接发言指令竞争的第二套规则。
    :param emoji_enabled: 本轮是否允许发表情包。
    :param emoji_tags: 表情包库内高频情绪标签，用于锚定 ``<emoji>`` 的 emotion 用词。
    :param jargon: 本轮消息命中的黑话 ``(词, 含义)`` 列表，由 ``agent/jargon.py``
        查表截断后传入；为空时整块省略。
    :param decision_only: 只产出动作决策、不写正文时置真。此时省略回复风格、
        临时语调与表达样本三块：它们只影响表达方式，对决策无用，保留会占用
        上下文并诱导模型直接产出台词。身份、人格、关系与记忆照常注入：
        动作决策依赖这些背景。

    :return: 可直接提交给模型服务的完整系统提示词。

    :raises ValueError: 生日文本不是合法 ISO 日期，或提示词资源缺失时由资源加载逻辑抛出。
    :raises TypeError: 上下文参数类型不符合字符串、序列或日期操作要求时抛出。

    副作用：
        ``now`` 省略时读取一次系统时钟；不修改传入的列表和配置对象。
        结果长度随记忆、活动和表达习惯文本线性增长。
    """

    if now is None:
        now = datetime.fromtimestamp(current_time() / 1000)

    identity, parsed_birthday = _identity_context(
        personality,
        birthday,
        now,
        aliases,
        platform_name,
        name,
    )
    birthday_note = ''
    if (
        parsed_birthday is not None
        and (parsed_birthday.month, parsed_birthday.day) == (now.month, now.day)
    ):
        birthday_note = '\n今天是你的生日。'

    component_values = {
        template_id: {}
        for template_id in CHAT_SYSTEM_COMPONENTS
    }
    component_values[CHAT_PROTOCOL_TEMPLATE_ID] = {
        'emotions': ' / '.join(EXPRESSION_IDS),
        'gestures': ' / '.join(GESTURE_IDS),
        'emoji_rule': _emoji_protocol_rule(emoji_enabled, emoji_tags),
    }
    if reply_length is not None:
        try:
            length_template_id = REPLY_LENGTH_TEMPLATE_IDS[reply_length]
        except KeyError as exc:
            raise ValueError(f'未知回复篇幅：{reply_length}') from exc
        component_values[length_template_id] = {}
    system_values = {
        'name': name,
        'identity': identity,
        'relationship': _relationship_block(acquaintance, user_nickname, relationship),
        'time_context': _time_context(now, schedule),
        'birthday_note': birthday_note,
        'resumption': _prefixed_block(resumption),
        'persona': _prefixed_block(persona),
        'activity': _activity_block(activity),
        'scene': _scene_block(scene),
        'jargon': _jargon_block(jargon),
        'impressions': _impressions_block(impressions),
        'facts': _facts_block(
            '你早就知道的事',
            facts,
            '这些是长期相处积累的常识，需要时自然使用，不需要时不提；不要逐条复述给对方。',
        ),
        'episodes': _memory_block(
            '最近留下的聊天回想',
            episodes,
            '回想只用来理解没说完的话和关系变化，不要为了证明记得而主动翻旧账。',
        ),
        # 决策层不写正文，表达层三块一并省略，见 decision_only 参数说明。
        'reply_style': '' if decision_only else reply_style,
        'tone': '' if decision_only else _prefixed_block(tone),
        # 表达样本放在靠近输出的位置：越贴近生成，模型越容易真正照着语感说话。
        'expression_habits': (
            '' if decision_only else _expression_habits_block(expression_habits)
        ),
    }
    prompt, system_values = render_chat_system(system_values, component_values)
    if protocol_text is not None:
        # 动作协议必须在系统提示词内整体替换「只输出 <say>」协议，不能在末尾追加。
        # 追加会让模型同时收到两条竞争指令，shadow 实测约 2/3 会退回直接输出 <say>。
        system_values = {**system_values, 'protocol': protocol_text.rstrip()}
        prompt = get_prompt('chat.system').render(**system_values)
    if render_params is not None:
        render_params.update(component_values)
        render_params['chat.system'] = system_values
    # 主骨架由资源模板决定；公共组装函数确保生产与重放使用完全相同的空白处理。
    return prompt


def build_itemized_system_prompt(
    name: str,
    birthday: str,
    personality: str,
    reply_style: str,
    now: Optional[datetime] = None,
    persona: Optional[str] = None,
    acquaintance: Optional[str] = None,
    facts: Optional[Sequence['str | MemoryFactItem']] = None,
    episodes: Optional[List[str]] = None,
    activity: Optional[str] = None,
    schedule: Optional[str] = None,
    user_nickname: Optional[str] = None,
    relationship: Optional[str] = None,
    expression_habits: Optional[str] = None,
    reply_length: Optional[str] = None,
    tone: Optional[str] = None,
    resumption: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    platform_name: Optional[str] = None,
    scene: Optional[Tuple[str, str]] = None,
    jargon: Optional[Sequence[Tuple[str, str]]] = None,
    impressions: Optional[Sequence[InjectionProfile]] = None,
    render_params: Optional[Dict[str, Dict[str, str]]] = None,
    decision_only: bool = False,
) -> Tuple[str, List[str]]:
    """把稳定系统规则与运行时上下文拆成可独立裁剪、观测的 item。

    工具调用模式不再依赖 assistant/user 角色交替：稳定身份、事实纪律与边界留在
    system，当前时间、人物画像、重逢背景、活动、场景和记忆分别渲染成独立文本项。
    动作或回复协议由调用方放在消息流末尾，避免它重新被嵌回 system。

    :param name: Bot 的主名称。
    :param birthday: ISO ``YYYY-MM-DD`` 格式生日；空字符串表示未配置。
    :param personality: 配置中的稳定人格与身份描述。
    :param reply_style: 回复风格约束；决策层会按 ``decision_only`` 省略。
    :param now: 当前本地时间；省略时读取统一时钟。
    :param persona: 当前人物关系与精力画像。
    :param acquaintance: 可选相识时长描述。
    :param facts: 可选长期事实记忆；条目为 ``MemoryFactItem`` 时，
        同一槽位下对不上的多条会并排渲染并明确标注。
    :param episodes: 可选近期聊天回想。
    :param activity: 可选前台活动背景。
    :param schedule: 可选当日日程，随时间项一起渲染。
    :param user_nickname: 对方偏好的称呼。
    :param relationship: 对方在 Bot 视角下的关系描述。
    :param expression_habits: 当前轮表达习惯样本。
    :param reply_length: 当前轮篇幅枚举；为空时不注入篇幅组件。
    :param tone: 当前轮临时语调。
    :param resumption: 久别后的恢复提示。
    :param aliases: 可选 Bot 别名。
    :param platform_name: 平台侧 Bot 显示名。
    :param scene: 可选群聊场景画像。
    :param jargon: 本轮消息命中的黑话 ``(词, 含义)`` 列表；为空时不生成该上下文项。
    :param render_params: 可选的提示词渲染参数收集字典。
    :param decision_only: 是否只做动作决策；为真时省略表达层内容。

    :return: ``(稳定 system 文本, 按语义拆分的运行时上下文文本列表)``。

    :raises ValueError: 生日或回复篇幅无效，或模板渲染参数不匹配。
    :raises KeyError: 必需提示词模板未加载。

    副作用：
        ``now`` 省略时读取一次统一时钟；可选地更新 ``render_params``，不修改其
        既有无关条目。
    """
    if now is None:
        now = datetime.fromtimestamp(current_time() / 1000)

    identity, parsed_birthday = _identity_context(
        personality,
        birthday,
        now,
        aliases,
        platform_name,
        name,
    )
    birthday_note = ''
    if (
        parsed_birthday is not None
        and (parsed_birthday.month, parsed_birthday.day) == (now.month, now.day)
    ):
        birthday_note = '今天是你的生日。'

    length = ''
    length_template_id: str | None = None
    if reply_length is not None:
        try:
            length_template_id = REPLY_LENGTH_TEMPLATE_IDS[reply_length]
        except KeyError as exc:
            raise ValueError(f'未知回复篇幅：{reply_length}') from exc
        length = get_prompt(length_template_id).render().rstrip()

    # 表达层整节在这里组装，而不是在模板里写死标题：decision_only 会把这一节的
    # 内容全部抽走，标题留在模板里就会渲染出一个空的「# 说话的风格」——决策层每
    # 轮都会读到一个空小节，并可能误以为遗漏了内容。
    sections: List[str] = []
    if not decision_only:
        voice = (
            f'{reply_style}'
            f'{_prefixed_block(tone)}'
            f'{_expression_habits_block(expression_habits)}'
        ).strip()
        if voice:
            sections.append('# 说话风格' + '\n' + voice)
    # 篇幅块自带标题，与表达层同级，单独成节而不是并进上面那段。
    if length:
        sections.append(length)
    voice_block = ('\n' * 2 + ('\n' * 2).join(sections)) if sections else ''

    system_values = {
        'name': name,
        'identity': identity,
        'relationship': _relationship_block(acquaintance, user_nickname, relationship),
        # 表达层三块与篇幅已经组装成整节；决策层不写正文，那一节整个不渲染。
        'voice': voice_block,
        'discipline': get_prompt('chat.discipline').render().rstrip(),
        'boundaries': get_prompt('chat.boundaries').render().rstrip(),
    }
    system = get_prompt('chat.item.system').render(**system_values)

    time_context = _time_context(now, schedule)
    if birthday_note:
        time_context = f'{time_context}\n{birthday_note}'
    context_values = [
        ('当前时间', time_context),
        ('人物画像', persona or ''),
        ('重逢背景', resumption or ''),
        ('当前活动', _activity_block(activity).strip()),
        ('会话场景', _scene_block(scene).strip()),
        ('群里的说法', _jargon_block(jargon).strip()),
        ('你对他们的印象', _impressions_block(impressions).strip()),
        ('长期记忆', _facts_block(
            '你早就知道的事',
            facts,
            '这些是长期相处积累的常识，需要时自然使用，不需要时不提；不要逐条复述给对方。',
        ).strip()),
        ('近期回想', _memory_block(
            '最近留下的聊天回想',
            episodes,
            '回想只用来理解没说完的话和关系变化，不要为了证明记得而主动翻旧账。',
        ).strip()),
    ]
    item_template = get_prompt('chat.context.item')
    items = [
        item_template.render(kind=kind, content=content.strip()).rstrip()
        for kind, content in context_values
        if content.strip()
    ]

    if render_params is not None:
        render_params.update({
            'chat.discipline': {},
            'chat.boundaries': {},
            'chat.item.system': system_values,
        })
        if length_template_id is not None:
            render_params[length_template_id] = {}
    return system, items


def build_proactive_prompt(
    base_prompt: str,
    situation: str,
    render_params: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    """在已有系统提示词后追加一次主动搭话场景描述。

    :param base_prompt: 已完成的人格和上下文系统提示词。
    :param situation: 触发主动搭话的当前场景描述。

    :return: 由基础提示词和主动搭话模板组成的新提示词。

    :raises KeyError: 主动搭话提示词资源未注册时抛出。
    :raises TypeError: 参数不是可拼接字符串时抛出。
    """

    proactive_values = {'situation': situation}
    if render_params is not None:
        render_params['chat.proactive'] = proactive_values
    return '\n\n'.join([
        base_prompt,
        get_prompt('chat.proactive').render(**proactive_values),
    ])

# 可选消息清单里每条原文的展示上限（字符）。清单只用于让模型认出编号对应
# 哪条消息，完整正文在历史里已经给过，这里超出上限即截断。
SELECTABLE_PREVIEW_LIMIT = 40


def _render_selectable_messages(items: List[Tuple[int, str]]) -> str:
    """把本回合可选消息渲染为「编号 = 原文」的锚点清单。

    Agent 上下文的历史已逐行带 ``[编号]`` 前缀，本清单在此之上再框定范围：
    历史里编号很多，但只有当前批次内的几条可以作为目标消息，越界同样按
    illegal_action 失败。两者缺一不可——只给清单则编号在历史里无处对应，
    只给历史则模型分不清哪几条是本回合可选的。

    :param items: ``(消息 ID, 展示原文)`` 序列，顺序即消息到达顺序。
    :return: 每行一条、带两空格缩进的清单文本；空序列返回占位说明。
    """
    if not items:
        return '  （本批没有可选消息）'
    lines: List[str] = []
    for message_id, preview in items:
        # 原文可能跨行（转发、多段消息），压成单行才不会撑散清单结构。
        single_line = ' '.join(preview.split())
        if len(single_line) > SELECTABLE_PREVIEW_LIMIT:
            single_line = f'{single_line[:SELECTABLE_PREVIEW_LIMIT]}…'
        lines.append(f'  {message_id} = {single_line}')
    return '\n'.join(lines)


def _render_turn_scope(target_person: str) -> str:
    """渲染「这一轮回应谁」的范围说明。

    可选清单只覆盖当前人物这一批消息，是缓冲按人物切批的结果，不是模型选错。
    线上观测到的全部 illegal_action 均为同一形态：模型尝试回应群里另一个人的
    消息，清单中无对应编号，把目标字段写成人名，整轮被判协议失败、表现为
    Bot 不回话。因此该说明须讲清两点：越界不可行；其他人的消息有各自的回合，
    无需在本轮处理。

    :param target_person: 本轮批次发送者的显示名；私聊传空字符串。
    :return: 供协议模板插入的单行说明。
    """
    who = f'{target_person}刚发送的消息' if target_person else '对方刚发送的消息'
    return (
        f'本轮只处理{who}，清单以外的编号（包括群里其他人刚发的）一律不能填写。'
        '其他人的消息会由各自属于他们的回合处理；如果你真正想回应的是其他人的消息、'
        '对清单中这几条没有需要回应的内容，请选择 silent。'
        '历史记录中其他人的消息仍然可以阅读，用于理解上下文，也可以在台词中顺带提及。'
    )


def render_action_protocol(
    available_actions: Iterable[str],
    selectable_messages: Iterable[Tuple[int, str]],
    quote_supported: bool,
    emoji_enabled: bool = False,
    emoji_tags: Sequence[str] = (),
    target_person: str = '',
    cognitive_rounds: int = 0,
    available_reactions: Sequence[str] = (),
    stream_kind: str = 'group',
) -> str:
    """渲染 Conversation Agent 的动作头协议提示词块。

    该文本整体替换主对话提示词中的直接发言协议，而不是追加在末尾：动作头
    先于正文是 Agent 模式的唯一输出格式，与 ``chat.protocol`` 的
    「直接输出 <say>」指令互斥。

    :param available_actions: 运行时给出的本回合动作枚举值。
    :param selectable_messages: 本回合可选消息的 ``(消息 ID, 展示原文)`` 序列；
        原文用于在提示词里给编号建立锚点，模型据此才能照抄出合法 targets。
    :param quote_supported: 平台是否支持模型在决策里显式指定引用目标；不支持时
        提示词明确禁止 quote。平台投递层的自动引用不受该开关控制。
    :param emoji_enabled: 本轮是否允许发表情包。
    :param emoji_tags: 表情包库内高频情绪标签，锚定 ``<emoji>`` 的 emotion 用词。
    :param target_person: 本回合批次发送者的显示名，用于说明这一轮在回应谁的消息；
        私聊传空字符串。
    :param cognitive_rounds: 本回合的认知轮次预算；写进提示词使模型预先知道
        检索次数上限。仅向模型复述动作空间的既有约束，实际约束在
        available_actions，两处口径必须一致。
    :param available_reactions: 本平台真实可用的表情回应标识；react 不在动作集时
        传空序列，此时该段完全不渲染。
    :param stream_kind: 会话类型；等待动作的收尾语义在群聊与私聊不同。

    :return: 已通过模板占位符严格校验的协议文本。
    :raises KeyError: 模板未加载时由注册表抛出。
    """
    actions_text = ' / '.join(sorted(available_actions))
    selectable = list(selectable_messages)
    ids = [message_id for message_id, _ in selectable]
    selectable_text = _render_selectable_messages(selectable)
    # 不写「本平台不支持引用」：QQ 群聊的回复由投递层按需要自动挂引用，
    # 断言平台没有引用能力会让 Bot 在台词里说出与事实相反的话。这条规则只约束
    # 动作头里能不能出现 quote 属性。
    quote_rule = (
        'quote 只能引用上面列出的可选消息之一；不引用就不写 quote 属性'
        if quote_supported
        else '不要写 quote 属性，需要指向哪一条由 targets 决定'
    )
    actions = frozenset(available_actions)
    # 示例按动作空间逐条开关：示例是模型最容易照抄的部分，展示一个本回合非法的
    # 动作等于主动制造 illegal_action。私聊与桌面不允许 silent，那里必须看不到
    # silent 示例；可选消息为空时同理不能给出 targets="0" 这种必然非法的目标。
    reply_example = (
        '\n# reply（回复）\n'
        f'<decision action="reply" targets="{str(ids[0])}" '
        'reasons="direct_question" length="brief"/>\n'
        '<say emotion="normal">嗯嗯，我看到了。</say>'
        '<say emotion="smile">你继续说。</say>\n'
    ) if 'reply' in actions and ids else ''
    silent_example = (
        '\n# silent（不回复）\n'
        '<decision action="silent" reasons="others_conversation"/>\n'
    ) if 'silent' in actions else ''
    return get_prompt('chat.action.protocol').render(
        available_actions=actions_text,
        selectable_messages=selectable_text,
        turn_scope=_render_turn_scope(target_person),
        quote_rule=quote_rule,
        emotions=' / '.join(EXPRESSION_IDS),
        gestures=' / '.join(GESTURE_IDS),
        reply_example=reply_example,
        silent_example=silent_example,
        emoji_rule=_emoji_protocol_rule(emoji_enabled, emoji_tags),
        cognition_rule=_cognition_protocol_rule(actions, ids, cognitive_rounds),
        react_rule=_react_protocol_rule(actions, ids, available_reactions),
        poke_rule=_poke_protocol_rule(actions, ids),
        wait_rule=_wait_protocol_rule(actions, stream_kind),
        speak_rule=_speak_protocol_rule(actions),
    )


def render_tool_protocol(
    selectable_messages: Iterable[Tuple[int, str]],
    quote_supported: bool,
    target_person: str = '',
    cognitive_rounds: int = 0,
    available_actions: Iterable[str] = (),
) -> str:
    """渲染工具调用模式下决策那一次的协议文本。

    与 XML 动作头协议互斥：动作枚举、参数取值、理由码分域全部由工具声明承载，
    本段仅保留必须由提示词承载的三件事：目标编号与原文的对应、能否写引用、
    篇幅选择口径。写入工具描述会使每个工具的 description 重复并稀释动作说明。

    :param selectable_messages: 本回合可选消息的 ``(消息 ID, 展示原文)`` 序列；
        工具声明里 target 是裸数字，缺少该对照时模型无法确定编号对应的消息。
    :param quote_supported: 平台是否支持模型显式指定引用目标。
    :param target_person: 本回合批次发送者的显示名；私聊传空字符串。
    :param cognitive_rounds: 本回合的认知轮次预算，用于渲染检索说明。
    :param available_actions: 本轮动作集，决定要不要渲染检索说明。
    :return: 已通过模板占位符严格校验的协议文本。
    :raises KeyError: 模板未加载时由注册表抛出。
    """
    selectable = list(selectable_messages)
    ids = [message_id for message_id, _ in selectable]
    quote_rule = (
        '需要点明在回哪一条时可以填 quote，取值同样只能来自上面的可选消息。'
        if quote_supported
        else '不要填 quote，需要指向哪一条由 target 决定。'
    )
    return get_prompt('chat.tool.protocol').render(
        turn_scope=_render_turn_scope(target_person),
        selectable_messages=_render_selectable_messages(selectable),
        quote_rule=quote_rule,
        cognition_rule=_cognition_protocol_rule(
            frozenset(available_actions), ids, cognitive_rounds, tool_mode=True,
        ),
    )


def render_replyer_protocol(
    reference: str,
    length: str | None,
    emoji_enabled: bool = False,
    emoji_tags: Sequence[str] = (),
) -> str:
    """渲染回复生成那一次调用的协议文本。

    与动作头协议互斥：决策已定，该段仅约束正文表达。它同样整体替换系统提示词
    里的直接发言协议，因此回复生成模型看到的人格、历史、事实与决策那次完全
    相同，区别只在这一段。

    :param reference: 决策层写的背景说明；为空时退回一句中性说明，不留空占位符：
        空白背景会使模型自行猜测开口原因。
    :param length: 决策层选定的篇幅；``None`` 时按 brief 处理，与单次调用路径
        ``to_decision`` 的默认口径一致。
    :param emoji_enabled: 本回合是否允许发表情包。
    :param emoji_tags: 表情包库内高频情绪标签，锚定 ``<emoji>`` 的 emotion 用词。
    :return: 已通过模板占位符严格校验的协议文本。
    :raises KeyError: 模板未加载时由注册表抛出。
    """
    return get_prompt('chat.replyer').render(
        reference=reference.strip() or '继续当前话题，不要开启新话题。',
        length_rule=_replyer_length_rule(length),
        emotions=' / '.join(EXPRESSION_IDS),
        gestures=' / '.join(GESTURE_IDS),
        emoji_rule=_emoji_protocol_rule(emoji_enabled, emoji_tags),
    )


def _replyer_length_rule(length: str | None) -> str:
    """把决策层选定的篇幅转换成给回复生成模型的具体要求。

    篇幅由决策层判定，此处仅向回复生成模型转达结论：两级各判一次会使
    短回复口径失效。
    """
    if length == 'long':
        return (
            '篇幅：把话说完整即可，不要写长，整轮不超过八九十个字。'
        )
    return (
        '篇幅：简短回复。允许句子残缺、省略主语、倒装，整轮合计二三十个字即可。'
    )


def _speak_protocol_rule(actions: FrozenSet[str]) -> str:
    """渲染「主动发起一个不回应任何人的话题」的说明。

    speak 与 reply 的区别只在有没有目标：reply 回应某条消息，speak 是 Bot 主动
    发起话题。speak 不需要独立的触发路径：扩展触发口径在群里活跃但无人点名
    Bot 时已给出候选，speak 为该候选增加一个动作选项。

    该段措辞重点为约束无内容时的强行发言：缺乏可说内容时强行发言会产出空泛
    搭话，主动发言效果差的根因通常在内容而非触发机制，因此反复强调无内容时
    不发言。

    :param actions: 本轮实际可用的动作集合。
    :return: 主动开口说明文本；speak 不可用时返回空字符串。
    """
    if 'speak' not in actions:
        return ''
    return '\n'.join([
        '',
        '如果清单中的消息你都不想回应，但确实有其他想说的内容，可以开启一个新话题——'
        '不回应任何人，是你自己想发言：',
        '<decision action="speak" reasons="理由码"/>',
        '<say emotion="表情">你想说的话</say>',
        '- reasons 只能写：noticed_activity（看到他们在聊的话题想参与）/ '
        'remembered_something（想起一件相关的事）/ '
        'long_silence（长时间没有发言）/ promise_due（之前答应过的事到了时间）',
        '- 不写 targets、length、quote——没有正在回应的消息',
        '- 主动发言保持简短，一句即可',
        '- 绝大多数时候都应选择 silent。没有需要表达的内容时不要发言：'
        '勉强发言、复述他人刚说过的内容、无实际内容的搭话，效果都比沉默差',
        '- 只在确实有可补充的内容（你知道对方不知道的信息、想起相关的事、'
        '话题适合参与）时才使用 speak',
    ]) + '\n'


def _wait_protocol_rule(actions: FrozenSet[str], stream_kind: str) -> str:
    """渲染「先等等」动作的说明。

    与 silent 的分界必须写清楚，否则模型会把两者当同义词：silent 是放弃这批消息，
    wait 是暂不表态、这批消息之后还会再进入回合。收尾语义按会话类型分化：
    提示词中出现另一种会话的措辞，会使模型把当前回合误判为该类型。

    :param actions: 本轮实际可用的动作集合。
    :param stream_kind: 会话类型；等待的收尾语义在群聊与私聊不同。
    :return: 等待说明文本；wait 不可用时返回空字符串。
    """
    if 'wait' not in actions:
        return ''
    if stream_kind == 'direct':
        ending = (
            '- 等不到后续时，这批消息稍后会再次进入你的回合，届时同样需要表态，'
            '不会被一直搁置'
        )
    else:
        ending = '- 等不到后续就视为不需要回应'
    return '\n'.join([
        '',
        '一个意思常被拆成多条消息连续发送。刚到的消息很短、语义不完整、或明显还有后续时，'
        '先不表态，等后续消息合并进来后统一回应，比逐条回复更自然：',
        '<decision action="wait" reasons="理由码"/>',
        '- reasons 只能写：unfinished_thought（对方的话没有说完）/ thread_developing（对话还在继续）',
        '- 不写 targets、length、quote，之后不要有任何正文',
        '- wait 与 silent 不同：silent 是放弃回应这批消息，wait 是在等待后续消息，'
        '这批消息之后还会再次进入你的回合',
        '- wait 只能使用一次。等过之后再看到这批消息时必须给出最终动作，届时不再有这个选项',
        ending,
    ]) + '\n'


def _poke_protocol_rule(
    actions: FrozenSet[str],
    selectable_ids: Sequence[int],
) -> str:
    """渲染戳一戳动作的说明。

    :param actions: 本轮实际可用的动作集合。
    :param selectable_ids: 本轮可选消息 ID，用于给示例挑一个合法目标。
    :return: 戳一戳说明文本；poke 不可用时返回空字符串。
    """
    if 'poke' not in actions:
        return ''
    lines = [
        '',
        '你还可以戳一戳某个人（QQ 的戳一戳，不发消息）：',
        '<decision action="poke" targets="消息编号" reasons="理由码"/>',
        '- targets 只填一条，写你想戳的那个人发的消息；不写 length、不写 quote、之后不要有正文',
        '它会给对方发送一条提醒。有人戳了你时，可以戳回去回应，不必每次都发消息；'
        '其余时候只在确实需要提醒对方时使用，不要频繁使用',
    ]
    if selectable_ids:
        lines.extend([
            '',
            '# 戳一戳的例子',
            f'<decision action="poke" targets="{selectable_ids[0]}" '
            'reasons="relationship_impulse"/>',
        ])
    return '\n'.join(lines) + '\n'


def _react_protocol_rule(
    actions: FrozenSet[str],
    selectable_ids: Sequence[int],
    available_reactions: Sequence[str],
) -> str:
    """渲染表情回应（react）的可用性与用法说明。

    与 reply / silent / 认知动作示例同一条纪律：动作集里没有 react 时整段不渲染。
    展示一个本回合非法的动作等同于主动制造 illegal_action。

    可用反应逐个列出而不由模型自由描述情绪：表情最终映射到平台的封闭编号，
    自由文本会使映射失败推迟到投递时才暴露。

    :param actions: 本轮实际可用的动作集合。
    :param selectable_ids: 本轮可选消息 ID，用于给示例挑一个合法目标。
    :param available_reactions: 本平台真实可用的反应标识。
    :return: 表情回应说明文本；react 不可用时返回空字符串。
    """
    if 'react' not in actions or not available_reactions:
        return ''
    lines = [
        '',
        '除了发言和沉默，你还可以给某条消息添加一个表情回应——'
        '即在他人消息上添加一个表情，不发送新消息：',
        f'<decision action="react" targets="消息编号" reaction="表情" reasons="理由码"/>',
        f'- reaction 只能写：{" / ".join(available_reactions)}',
        '- targets 只填一条，写你在回应哪条消息；不写 length、不写 quote、之后不要有任何正文',
        '- 需要发言时正常使用 reply；表情回应适合「某条消息值得回应、但你不想开口」的情况',
    ]
    if selectable_ids:
        lines.extend([
            '',
            '# 表情回应的例子',
            f'<decision action="react" targets="{selectable_ids[0]}" '
            f'reaction="{available_reactions[0]}" reasons="natural_reaction"/>',
        ])
    return '\n'.join(lines) + '\n'


def _cognition_protocol_rule(
    actions: FrozenSet[str],
    selectable_ids: Sequence[int],
    cognitive_rounds: int,
    tool_mode: bool = False,
) -> str:
    """渲染本轮认知动作（recall / inspect / consult）的可用性与用法说明。

    认知动作只在本回合还剩检索次数时进入动作空间，因此本段按实际动作集渲染：
    动作集之外的动作不得出现在提示词里，展示一个本回合非法的动作等同于制造
    illegal_action，与 reply/silent 示例的开关同一纪律。

    该段强调绝大多数时候无需检索：每次检索是一次完整的模型往返，直接增加
    首字延迟。检索应由信息缺失触发，不应由保险起见触发。

    :param actions: 本轮实际可用的动作集合。
    :param selectable_ids: 本轮可选消息 ID；用于给示例挑一个合法的后续目标。
    :param cognitive_rounds: 本回合的检索次数上限，写进说明避免 Bot 在最后一轮
        还想再查（那一轮认知动作已不在动作空间里，会被判为协议失败）。
    :return: 认知动作说明文本；本轮不含认知动作时返回空字符串。
    """
    available = sorted(actions & COGNITIVE_ACTIONS)
    if not available:
        return ''
    lines = [
        '',
        '上面的聊天记录只是你们此刻的互动，你和这些人之间还有更多过去的事和你知道的'
        '知识没有出现在上下文中。想不起来或拿不准时，可以先查询再决定本轮做什么：',
    ]
    if tool_mode:
        # 工具模式下检索动作的调用形状由函数签名承载，这里只讲什么时候用它。
        # 再写一遍 XML 语法会让模型以为还有第二套输出格式。
        if 'recall' in available:
            lines.append(
                '- 查询你的长期记忆，包括你记得的关于在场这些人的事，'
                '以及你们共同经历过的事'
            )
        if 'inspect' in available:
            lines.append('- 查询这个会话更早的聊天记录，即上面聊天记录之前发生的内容')
        if 'consult' in available:
            lines.append(
                '- 查询你知道的知识和资料，例如概念、定义、事实——这不是查询聊天记录'
            )
        lines.extend([
            f'- 本回合你最多只能查询 {cognitive_rounds} 次，查完必须给出最终动作',
            '- 绝大多数时候都不需要查询，直接给出最终动作。只有两类情况值得查：'
            '对方话里带「上次」「之前」「还记得吗」「我说过」这类指过去的信号，'
            '或者话题明显指向你看不到的更早内容',
            '- 查询没有结果时，按眼前的消息正常回复，不要编造记忆，'
            '也不要向对方提到查询过程',
        ])
        return '\n'.join(lines) + '\n'
    if 'recall' in available:
        lines.append(
            '- <decision action="recall" query="想查的东西"/>：'
            '查询你的长期记忆，包括你记得的关于在场这些人的事，以及你们共同经历过的事'
        )
    if 'inspect' in available:
        lines.append(
            '- <decision action="inspect" query="想查的东西"/>：'
            '查询这个会话更早的聊天记录，即上面聊天记录之前发生的内容'
        )
    if 'consult' in available:
        lines.append(
            '- <decision action="consult" query="想查的东西"/>：'
            '查询你知道的知识和资料，例如概念、定义、事实——这不是查询聊天记录'
        )
    lines.extend([
        '- query 必填，写你想查什么，用几个关键词即可；'
        '这些动作都不写 targets、reasons、length、quote',
        '- 查询结果会在下一轮提供给你，届时再决定是否回复以及回复内容；动作标签之后不要写任何正文',
        f'- 本回合你最多只能查询 {cognitive_rounds} 次，查完必须给出最终动作',
        '- 绝大多数时候都不需要查询，直接给出最终动作。只有两类情况值得查：'
        '对方话里带「上次」「之前」「还记得吗」「我说过」这类指过去的信号，'
        '或者话题明显指向你看不到的更早内容',
        '- 查询没有结果时，按眼前的消息正常回复，不要编造记忆，'
        '也不要向对方提到查询过程',
    ])
    if selectable_ids:
        lines.extend([
            '',
            '# 先查再回的例子',
            f'<decision action="{available[0]}" query="对方刚才提到的那个概念"/>',
            '（收到检索结果之后，下一轮再写 '
            f'<decision action="reply" targets="{selectable_ids[0]}" '
            'reasons="pending_thread" length="brief"/> 和台词）',
        ])
    return '\n'.join(lines) + '\n'


def _emoji_protocol_rule(enabled: bool, tags: Sequence[str] = ()) -> str:
    """渲染当前平台和频率窗口对应的表情包可见产物规则。

    :param enabled: 本轮是否允许发表情包。
    :param tags: 库内高频情绪标签，锚定 emotion 的用词，降低检索落空率；
        为空时退回自由措辞口径。
    :return: 供协议模板 ``{{emoji_rule}}`` 占位符使用的规则文本。
    """

    if not enabled:
        return '本轮不支持发送表情包，不要写 <emoji> 标签。'
    vocabulary = (
        f'emotion 优先从这些库里常备的词里挑最贴的：{"、".join(tags)}。'
        if tags
        else 'emotion 写简短的情绪词。'
    )
    return (
        '当情绪需要表情包辅助表达、或需要回应对方发送的表情包时，在 <say> 之后追加'
        '最多一个 <emoji emotion="目标情绪"/>；也允许只发送一个 <emoji> 而不写 '
        f'<say>，但两者不能都没有。{vocabulary}'
        '库中没有贴切的词时再自行措辞；不要连续多轮发送表情包。'
    )
