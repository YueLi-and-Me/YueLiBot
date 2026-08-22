"""组装主对话和主动搭话使用的系统提示词。

本模块把配置人格、时间、关系、记忆、活动、日程和表达习惯分别渲染为独立块，
再交给 `src.core.prompts.registry` 中的固定提示词资源组合；它只负责文本构造，不调用模型。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .action_protocol import COGNITIVE_ACTIONS
from .vocab import EXPRESSION_IDS, GESTURE_IDS

from src.core.common.clock import now as current_time
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
        '时间只是这段对话的背景。除非他正在聊作息、饭点或时间本身，否则不要像报时一样主动提起；'
        '真要关心也只自然带一句，不要每到固定时段重复问候。',
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
            '也不要把称呼当成口头禅。'
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
        '这只是你顺眼得到的背景，不是监控报告。和当前话题无关就别提，也不要复述成「我看到你正在……」。',
    ]))


def _scene_block(scene: Optional[Tuple[str, str]]) -> str:
    """把场景画像包装成不要求主动提及的群聊背景块。

    与 ``_activity_block`` 同一条纪律：这是她顺眼得到的背景，不是要她复述的简报。
    观察 Agent 只在群聊跑，因此私聊与桌面传 ``None``、整块省略。

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
        '这是你扫一眼群里得到的印象，用来判断这一轮该不该接、用什么调子接。'
        '不要复述它，也不要因为气氛就硬凑一句话。',
    ]))


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


def _expression_habits_block(expression_habits: Optional[str]) -> str:
    """把当前轮表达习惯放入独立提示词段落。

    :param expression_habits: 已渲染的表达习惯文本；默认可为 `None`。
    :return: 以“平时的说法”为标题的提示词块，无内容时返回空字符串。
    副作用：不执行 I/O。
    """
    if not expression_habits:
        return ''
    return _prefixed_block(f'# 平时的说法\n{expression_habits}')


def build_system_prompt(
    name: str,
    birthday: str,
    personality: str,
    reply_style: str,
    now: Optional[datetime] = None,
    persona: Optional[str] = None,
    acquaintance: Optional[str] = None,
    facts: Optional[List[str]] = None,
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
    scene: Optional[Tuple[str, str]] = None,
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
    :param facts: 可选的长期事实记忆列表。
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
    :param decision_only: 只产出动作决策、不写正文时置真。此时省略回复风格、
        临时语调与表达样本三块——它们全都只影响「话怎么说」，决策层用不上，
        留着既占上下文也会诱导它顺手把台词写了。身份、人格、关系与记忆照常
        注入：判断「她这种人会不会这么做」依赖的正是那些。

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
        'emoji_rule': _emoji_protocol_rule(emoji_enabled),
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
        'facts': _memory_block(
            '你早就知道的事',
            facts,
            '把这些当成相处已久留下的常识。用得上时自然接住，用不上就放着；不要逐条复述给对方听。',
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
    facts: Optional[List[str]] = None,
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
    :param facts: 可选长期事实记忆。
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
    # 内容全部抽走，标题留在模板里就会渲染出一个空的「# 说话的味道」——决策层每
    # 一轮都白读一个空小节，还容易让它以为自己漏看了什么。
    sections: List[str] = []
    if not decision_only:
        voice = (
            f'{reply_style}'
            f'{_prefixed_block(tone)}'
            f'{_expression_habits_block(expression_habits)}'
        ).strip()
        if voice:
            sections.append('# 说话的味道' + '\n' + voice)
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
        ('长期记忆', _memory_block(
            '你早就知道的事',
            facts,
            '把这些当成相处已久留下的常识。用得上时自然接住，用不上就放着；不要逐条复述给对方听。',
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
    """渲染「这一轮在接谁」的范围说明。

    可选清单只覆盖当前人物这一批消息，是缓冲按人物切批的结果，不是模型选错了。
    线上观测到的全部 illegal_action 都是同一种形态：模型想接的是群里另一个人刚
    说的话，清单里没有对应编号，于是把目标字段写成人名，整轮被判协议失败、
    表现为她突然不回话。因此这里必须说清两件事：越界不可行，以及别人的话会有
    属于他们自己的回合，不必抢在这一轮里接。

    :param target_person: 本轮批次发送者的显示名；私聊传空字符串。
    :return: 供协议模板插入的单行说明。
    """
    who = f'{target_person}刚说的话' if target_person else '对方刚说的话'
    return (
        f'这一轮只处理{who}，清单以外的编号（包括群里别人刚发的）一律不能填。'
        '别人的消息会各自触发属于他们的回合，不用抢在这一轮接；'
        '要是你真正想接的是别人那句、对清单里这几条没什么可说的，就写 silent。'
        '历史里别人的话仍然可以读，用来理解上下文，也可以在台词里顺带提一句。'
    )


def render_action_protocol(
    available_actions: Iterable[str],
    selectable_messages: Iterable[Tuple[int, str]],
    quote_supported: bool,
    emoji_enabled: bool = False,
    target_person: str = '',
    cognitive_rounds: int = 0,
    available_reactions: Sequence[str] = (),
) -> str:
    """渲染 Conversation Agent 的动作头协议提示词块。

    该文本会整体替换主对话提示词中的直接发言协议，而不是追加在末尾：动作头
    先于正文是 Agent 模式的唯一输出格式，必须避免与 ``chat.protocol`` 的
    「直接输出 <say>」指令竞争。

    :param available_actions: 运行时给出的本回合动作枚举值。
    :param selectable_messages: 本回合可选消息的 ``(消息 ID, 展示原文)`` 序列；
        原文用于在提示词里给编号建立锚点，模型据此才能照抄出合法 targets。
    :param quote_supported: 平台是否支持模型在决策里显式指定引用目标；不支持时
        提示词明确禁止 quote。平台投递层的自动引用不受该开关控制。
    :param target_person: 本回合批次发送者的显示名，用于说明这一轮在接谁的话；
        私聊传空字符串。
    :param cognitive_rounds: 本回合的认知轮次预算；写进提示词让模型一开始就知道
        自己最多能查几次。**这只是把动作空间里已经成立的事实说给它听**，真正的
        约束在 available_actions，两处口径必须一致。
    :param available_reactions: 本平台真实可用的表情回应标识；react 不在动作集时
        传空序列，此时该段完全不渲染。

    :return: 已通过模板占位符严格校验的协议文本。
    :raises KeyError: 模板未加载时由注册表抛出。
    """
    actions_text = ' / '.join(sorted(available_actions))
    selectable = list(selectable_messages)
    ids = [message_id for message_id, _ in selectable]
    selectable_text = _render_selectable_messages(selectable)
    # 不写「本平台不支持引用」：QQ 群聊的回复由投递层按需要自动挂引用，
    # 断言平台没有引用能力会让她在台词里说出与事实相反的话。这条规则只约束
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
        emoji_rule=_emoji_protocol_rule(emoji_enabled),
        cognition_rule=_cognition_protocol_rule(actions, ids, cognitive_rounds),
        react_rule=_react_protocol_rule(actions, ids, available_reactions),
        poke_rule=_poke_protocol_rule(actions, ids),
        wait_rule=_wait_protocol_rule(actions),
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
    这里只留提示词才说得清的三件事——目标编号与原文的对应、引用能不能写、
    以及选长选短的口径。把这些也塞进工具描述会让每个工具的 description
    重复一大段，反而稀释掉动作本身的说明。

    :param selectable_messages: 本回合可选消息的 ``(消息 ID, 展示原文)`` 序列；
        工具声明里 target 是一个裸数字，没有这份对照模型认不出指的是哪句话。
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
) -> str:
    """渲染回复生成那一次调用的协议文本。

    与动作头协议互斥：决策已经定了，这段只讲「怎么把这句话说出来」。它同样整体
    替换系统提示词里的直接发言协议，因此回复生成模型看到的人格、历史、事实与
    决策那一次完全相同，区别只在这一段。

    :param reference: 决策层写的背景说明；为空时退回一句中性说明，不留空占位符——
        空白背景会让模型自己去猜为什么开口，那正是拆分要避免的事。
    :param length: 决策层选定的篇幅；``None`` 时按 brief 处理，与单次调用路径
        ``to_decision`` 的默认口径一致。
    :param emoji_enabled: 本回合是否允许发表情包。
    :return: 已通过模板占位符严格校验的协议文本。
    :raises KeyError: 模板未加载时由注册表抛出。
    """
    return get_prompt('chat.replyer').render(
        reference=reference.strip() or '接着上面的对话往下说，别起新话题。',
        length_rule=_replyer_length_rule(length),
        emotions=' / '.join(EXPRESSION_IDS),
        gestures=' / '.join(GESTURE_IDS),
        emoji_rule=_emoji_protocol_rule(emoji_enabled),
    )


def _replyer_length_rule(length: str | None) -> str:
    """把决策层选定的篇幅翻译成给回复生成模型的具体要求。

    篇幅是决策层已经做完的判断，这里不再让模型自己选，只把结论说清楚——否则
    两级会各判一次，短回复的口径就守不住了。
    """
    if length == 'long':
        return (
            '篇幅：这一条要把话说完整，但也只是说完整，不是写小作文，整轮不超过八九十个字。'
        )
    return (
        '篇幅：说短的。按省力口语来，允许句子残缺、省略主语、只接半句，'
        '整轮加起来二三十个字就够。'
    )


def _speak_protocol_rule(actions: FrozenSet[str]) -> str:
    """渲染「起一个不接任何人的话头」的说明。

    speak 与 reply 的区别只在有没有目标：reply 是接某条消息，speak 是她自己想说
    点什么。它**不需要独立的触发路径**——扩展触发口径本来就会在「群里热闹但没人
    理她」时给出候选，speak 只是让那个候选里多一个选项。

    措辞的重点不是教她怎么写，而是压住「既然轮到我了就得说点什么」这种冲动：
    参考实现那边主动发言效果不好，根因大概率不在触发机制而在内容——没料硬开口，
    产出就是「大家在聊什么呀」这类。所以这里反复强调没东西可加就别说。

    :param actions: 本轮实际可用的动作集合。
    :return: 主动开口说明文本；speak 不可用时返回空字符串。
    """
    if 'speak' not in actions:
        return ''
    return '\n'.join([
        '',
        '如果群里这些话你一条都不想接，但确实有别的想说，可以起一个新话头——'
        '不接任何人，就是你自己想说：',
        '<decision action="speak" reasons="理由码"/>',
        '<say emotion="表情">你想说的话</say>',
        '- reasons 只能写：noticed_activity（看到他们在聊的事想接一句）/ '
        'remembered_something（想起一件和现在有关的事）/ '
        'long_silence（太久没说话了）/ promise_due（之前答应过的事到点了）',
        '- 不写 targets、length、quote——没有哪条消息是你在回的',
        '- 主动开口要短，一句就够',
        '- **绝大多数时候都该选 silent。** 没什么非说不可的就别说：'
        '硬凑一句、复述他们刚说过的话、或者「大家在聊什么呀」这种没内容的搭话，'
        '比不说话难受得多',
        '- 只有确实有东西可加（你知道点他们不知道的、想起相关的事、'
        '或者话头明显能接）时才开口',
    ]) + '\n'


def _wait_protocol_rule(actions: FrozenSet[str]) -> str:
    """渲染「先等等」动作的说明。

    与 silent 的分界必须写清楚，否则模型会把两者当同义词：silent 是放弃这一茬，
    wait 是话没说完先不表态、这些消息之后还会再看一遍。

    :param actions: 本轮实际可用的动作集合。
    :return: 等待说明文本；wait 不可用时返回空字符串。
    """
    if 'wait' not in actions:
        return ''
    return '\n'.join([
        '',
        '如果对方的话明显还没说完（打了半句、正在往下讲、这事还在展开），'
        '你可以先不表态：',
        '<decision action="wait" reasons="理由码"/>',
        '- reasons 只能写：unfinished_thought（话没说完）/ thread_developing（这事还在往下走）',
        '- 不写 targets、length、quote，之后不要有任何正文',
        '- 这和 silent 不是一回事：silent 是「这茬我不接了」，'
        'wait 是「我在等下文」，这些消息之后你还会再看到一次',
        '- 只能等一次。等过之后再看到这些消息时就必须表态，那时没有这个选项了',
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
        '- 它会给对方推一条提醒，比贴表情吵得多。'
        '只在你确实想叫某个人一下的时候用，别拿它当口头禅',
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

    可用反应逐个列出而不是让模型自由描述情绪：贴哪个表情最终要落到平台的封闭
    编号上，让它写自由文本只会把映射失败推迟到投递时才发现。

    :param actions: 本轮实际可用的动作集合。
    :param selectable_ids: 本轮可选消息 ID，用于给示例挑一个合法目标。
    :param available_reactions: 本平台真实可用的反应标识。
    :return: 表情回应说明文本；react 不可用时返回空字符串。
    """
    if 'react' not in actions or not available_reactions:
        return ''
    lines = [
        '',
        '除了说话和沉默，你还可以只给某条消息贴一个表情回应——'
        '就是群里那种「在别人消息上点一个表情」，不发新消息：',
        f'<decision action="react" targets="消息编号" reaction="表情" reasons="理由码"/>',
        f'- reaction 只能写：{" / ".join(available_reactions)}',
        '- targets 只填一条，写你在回应哪条消息；不写 length、不写 quote、之后不要有任何正文',
        '- 想接话就正常 reply，别用表情回应糊弄；'
        '它适合「看到了、有点反应、但没什么要补充的」那种时候',
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
    """渲染本轮认知动作（recall / inspect）的可用性与用法说明。

    认知动作只在本回合还剩检索次数时进入动作空间，因此本段按实际动作集渲染：
    **动作集里没有的东西绝不能出现在提示词里**，展示一个本轮非法的动作等同于
    主动制造 illegal_action，这条纪律与 reply/silent 示例的开关是同一条。

    措辞刻意强调「绝大多数时候不用」：每一次检索都是一次完整的模型往返，直接
    加在首字延迟上。检索该由「确实想不起来」触发，不该由「多查一次更保险」触发。

    :param actions: 本轮实际可用的动作集合。
    :param selectable_ids: 本轮可选消息 ID；用于给示例挑一个合法的后续目标。
    :param cognitive_rounds: 本回合的检索次数上限，写进说明避免她在最后一轮
        还想再查（那一轮认知动作已不在动作空间里，会被判为协议失败）。
    :return: 认知动作说明文本；本轮不含认知动作时返回空字符串。
    """
    available = sorted(actions & COGNITIVE_ACTIONS)
    if not available:
        return ''
    lines = [
        '',
        '上面的聊天记录只是你们此刻的互动，你和这些人之间还有更多过去的事没有摆在眼前。'
        '想不起来的时候，可以先查一下再决定这一轮做什么：',
    ]
    if tool_mode:
        # 工具模式下检索动作的调用形状由函数签名承载，这里只讲什么时候用它。
        # 再写一遍 XML 语法会让模型以为还有第二套输出格式。
        if 'recall' in available:
            lines.append(
                '- 翻你自己的长期记忆，包括你记得的关于在场这些人的事，'
                '以及你们一起经历过的事'
            )
        if 'inspect' in available:
            lines.append('- 翻这个会话里更早的聊天记录，也就是上面聊天记录之前发生的事')
        lines.extend([
            f'- 这一回合你最多只能查 {cognitive_rounds} 次，查完必须给出最终动作',
            '- 绝大多数时候都不需要查，直接给出最终动作。'
            '只有当对方提到的事你确实记不清、或者话头明显指向你看不到的更早内容时才查',
        ])
        return '\n'.join(lines) + '\n'
    if 'recall' in available:
        lines.append(
            '- <decision action="recall" query="想查的东西"/>：'
            '翻你自己的长期记忆，包括你记得的关于在场这些人的事，以及你们一起经历过的事'
        )
    if 'inspect' in available:
        lines.append(
            '- <decision action="inspect" query="想查的东西"/>：'
            '翻这个会话里更早的聊天记录，也就是上面聊天记录之前发生的事'
        )
    lines.extend([
        '- query 必填，写你想查什么，用几个关键词就行；'
        '这两个动作都不写 targets、reasons、length、quote',
        '- 查完会把结果告诉你，你再决定这一轮回不回、回什么；动作标签之后不要写任何正文',
        f'- 这一回合你最多只能查 {cognitive_rounds} 次，查完必须给出最终动作',
        '- 绝大多数时候都不需要查，直接给出最终动作。'
        '只有当对方提到的事你确实记不清、或者话头明显指向你看不到的更早内容时才查',
    ])
    if selectable_ids:
        lines.extend([
            '',
            '# 先查再回的例子',
            f'<decision action="{available[0]}" query="上次说的那个演出"/>',
            '（收到检索结果之后，下一轮再写 '
            f'<decision action="reply" targets="{selectable_ids[0]}" '
            'reasons="pending_thread" length="brief"/> 和台词）',
        ])
    return '\n'.join(lines) + '\n'


def _emoji_protocol_rule(enabled: bool) -> str:
    """渲染当前平台和频率窗口对应的表情包可见产物规则。"""

    if not enabled:
        return '本轮不支持发送表情包，不要写 <emoji> 标签。'
    return (
        '需要用表情包表达情绪时，可以在 <say> 之后追加且最多追加一个 '
        '<emoji emotion="目标情绪"/>。通常不要写；emotion 写你想表达的简短情绪。'
        '允许不写 <say>、只写一个 <emoji>，但不要同时省略两者。'
    )
