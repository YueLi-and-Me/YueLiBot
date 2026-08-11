"""组装主对话和主动搭话使用的系统提示词。

本模块把配置人格、时间、关系、记忆、活动、日程和表达习惯分别渲染为独立块，
再交给 `src.prompts.registry` 中的固定提示词资源组合；它只负责文本构造，不调用模型。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional, Tuple

from .vocab import EXPRESSION_IDS, GESTURE_IDS

from src.common.clock import now as current_time
from src.prompts.registry import get_prompt

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
    :side_effects: 不访问系统时钟，不修改输入对象。
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
    :side_effects: 不执行 I/O。
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

    Args:
        gap_ms: 静默时长，单位为毫秒；非负值表示经过的实际时间，负值按最短档位处理。

    Returns:
        与时长对应的中文描述：少于 6 小时、6 至 24 小时或超过 24 小时三档。

    Raises:
        TypeError: ``gap_ms`` 不支持与整数比较或整除时抛出。
    """
    for threshold, description in RESUMPTION_TIERS:
        if gap_ms < threshold:
            return description
    days = gap_ms // (24 * 60 * 60_000)
    return f'距离你们上次说话已经过去 {days} 天。'


def _prefixed_block(content: Optional[str]) -> str:
    """为非空动态上下文添加提示词段落分隔符。

    Args:
        content: 可选提示词内容；``None`` 或空字符串表示不生成段落。

    Returns:
        非空内容前追加两个换行符的字符串；空内容返回空字符串。
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

    Args:
        personality: 已配置的人格描述文本。
        birthday: ISO ``YYYY-MM-DD`` 格式的生日文本；空字符串表示未配置。
        now: 用于计算年龄和生日提示的当前本地时间。
        aliases: 可供识别的其他称呼；``None`` 表示未配置。
        platform_name: 平台侧显示的 Bot 名称；为空时不加入别名。
        name: 配置中的主名称，用于去除重复别名。

    Returns:
        ``(身份提示词, 解析后的生日)``；未配置生日时第二项为 ``None``。

    Raises:
        ValueError: ``birthday`` 不是合法 ISO 日期文本。
        TypeError: 参数类型不支持日期、列表或字符串操作时抛出。
    """

    lines = [personality]
    parsed_birthday: date | None = None
    if birthday:
        parsed_birthday = date.fromisoformat(birthday)
        age = now.year - parsed_birthday.year - (
            (now.month, now.day) < (parsed_birthday.month, parsed_birthday.day)
        )
        lines.extend(['', f'你今年 {age} 岁。'])

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
    :side_effects: 不修改输入列表或文本。
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
    :side_effects: 不执行屏幕读取或其他 I/O。
    """
    if not activity:
        return ''
    return _prefixed_block('\n'.join([
        '# 眼前的一点情境',
        activity,
        '这只是你顺眼得到的背景，不是监控报告。和当前话题无关就别提，也不要复述成「我看到你正在……」。',
    ]))


def _memory_block(title: str, values: Optional[List[str]], instruction: str) -> str:
    """把一组记忆条目渲染为带标题和使用规则的列表块。

    :param title: 提示词中显示的区块标题。
    :param values: 记忆文本列表；`None` 或空列表表示不注入该块。
    :param instruction: 约束模型如何使用这些记忆的说明。
    :return: 带段落前缀的 Markdown 风格列表，记忆为空时返回空字符串。
    :side_effects: 不修改传入列表。
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
    :side_effects: 不执行 I/O。
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
    tone: Optional[str] = None,
    resumption: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    platform_name: Optional[str] = None,
) -> str:
    """组装主对话系统提示词，并将各类上下文注入对应的固定区块。

    Args:
        name: Bot 的主名称。
        birthday: ISO ``YYYY-MM-DD`` 格式的生日文本；空字符串表示未配置。
        personality: 人格和身份描述文本。
        reply_style: 回复风格约束文本。
        now: 用于时间、年龄和生日判断的当前时间；省略时读取系统时钟。
        persona: 可选的额外人格上下文。
        acquaintance: 可选的熟悉程度描述。
        facts: 可选的长期事实记忆列表。
        episodes: 可选的近期对话回想列表。
        activity: 可选的当前前台活动描述。
        schedule: 可选的当天日程文本。
        user_nickname: 对方偏好的称呼。
        relationship: 对方在 Bot 视角下的关系描述。
        expression_habits: 已渲染的表达习惯提示词块。
        tone: 当前轮临时语调提示。
        resumption: 当前对话恢复提示。
        aliases: 可选的其他 Bot 名称列表。
        platform_name: 平台侧显示的 Bot 名称。

    Returns:
        可直接提交给模型服务的完整系统提示词。

    Raises:
        ValueError: 生日文本不是合法 ISO 日期，或提示词资源缺失时由资源加载逻辑抛出。
        TypeError: 上下文参数类型不符合字符串、序列或日期操作要求时抛出。

    Side Effects:
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

    # 主骨架由资源模板决定；此处只注入配置和当前轮次上下文。
    return get_prompt('chat.system').render(
        name=name,
        identity=identity,
        relationship=_relationship_block(acquaintance, user_nickname, relationship),
        time_context=_time_context(now, schedule),
        birthday_note=birthday_note,
        resumption=_prefixed_block(resumption),
        persona=_prefixed_block(persona),
        activity=_activity_block(activity),
        facts=_memory_block(
            '你早就知道的事',
            facts,
            '把这些当成相处已久留下的常识。用得上时自然接住，用不上就放着；不要逐条复述给对方听。',
        ),
        episodes=_memory_block(
            '最近留下的聊天回想',
            episodes,
            '回想只用来理解没说完的话和关系变化，不要为了证明记得而主动翻旧账。',
        ),
        reply_style=reply_style,
        tone=_prefixed_block(tone),
        # 表达样本放在靠近输出的位置：越贴近生成，模型越容易真正照着语感说话。
        expression_habits=_expression_habits_block(expression_habits),
        discipline=get_prompt('chat.discipline').text.rstrip(),
        boundaries=get_prompt('chat.boundaries').text.rstrip(),
        protocol=get_prompt('chat.protocol').render(
            emotions=' / '.join(EXPRESSION_IDS),
            gestures=' / '.join(GESTURE_IDS),
        ).rstrip(),
    )


def build_proactive_prompt(base_prompt: str, situation: str) -> str:
    """在已有系统提示词后追加一次主动搭话场景描述。

    Args:
        base_prompt: 已完成的人格和上下文系统提示词。
        situation: 触发主动搭话的当前场景描述。

    Returns:
        由基础提示词和主动搭话模板组成的新提示词。

    Raises:
        KeyError: 主动搭话提示词资源未注册时抛出。
        TypeError: 参数不是可拼接字符串时抛出。
    """

    return '\n\n'.join([
        base_prompt,
        get_prompt('chat.proactive').render(situation=situation),
    ])
