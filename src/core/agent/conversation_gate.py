"""三态门控：DROP / FORCE / DELIBERATE。

确定门控只负责硬边界与注意力过滤，不判断回复意愿。DROP
只处理确定、无语义争议的过滤（Bot 自己的消息、休眠、频率硬上限、无任何
注意力信号的群聊噪声）；FORCE 保证用户发起的私聊、桌面交互与 @必回必须
回应且不允许 silent；DELIBERATE 把普通群候选交给 Conversation Agent 自主选择。

轻量注意力信号只决定「是否进入意识」，不决定「是否回复」：名字/别名、回复
Bot、未决线索、进行中的话题、明确问题、自然回应窗口等信号将普通群聊批次
抬入 DELIBERATE，其余无信号批次在成形前即被 DROP。名字与别名全部由调用方
从 Bot 配置动态提供（通过 name_mentioned 事实传入），本模块不读取配置、
不写死任何称呼。

依赖：src.core.agent.action_protocol 的 GateDisposition 与
src.core.platform_io.types 的 StreamKind；被平台入口与聊天
编排服务调用，不访问数据库、不调用模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .action_protocol import GateDisposition

from src.core.platform_io.types import StreamKind

# DROP 原因：全部是确定、无语义争议的硬过滤，不承载回复意愿判断。
DROP_GATE_CODES: frozenset[str] = frozenset({
    'self_message',             # Bot 自己发的消息
    'poke_repeat',              # 同一 stream 的戳一戳在信号窗口内超出上限
    'asleep',                   # 休眠
    'rate_limited',             # 频率窗口硬上限
    'attention_filtered',       # 无任何注意力信号，不值得进入意识
    'frequency_wait',           # 频率预算尚未攒够候选消息
    'low_necessity',            # 回复必要性评分未达阈值
    'consumed',                 # 已被消费的重复消息
    'timeout_window',           # 超出超时窗
    'message_type_disallowed',  # 配置禁止的消息类型
})

# FORCE 原因：用户直接发起对话或 @必回，模型没有 silent 选项。
FORCE_GATE_CODES: frozenset[str] = frozenset({
    'at_mention_must_reply',  # 真实 @ 且 @必回开启
    'direct_conversation',    # QQ 私聊与 WebUI 桌面直接交互
    'system_confirmation',    # 明确的系统确认或用户操作结果
})

# DELIBERATE 原因：轻量注意力信号，仅决定「进入意识」，不决定「是否回复」。
DELIBERATE_GATE_CODES: frozenset[str] = frozenset({
    'name_mention',            # 出现名字/别名但无真实 @
    'direct_mention',          # 真实 @ 但 @必回 未开启
    'direct_poke',             # 有人戳了 Bot 自己（信号窗口内未超上限的那几次）
    'direct_emoji_like',       # 有人给 Bot 的消息贴了表情回应
    'reply_to_bot',            # 回复了 Bot 的消息
    'ongoing_topic',           # Bot 正在参与的话题在继续
    'pending_thread',          # 存在当前人物的未决线索
    'clear_question',          # 明确问题
    'natural_reply_window',    # Bot 最近发言后的自然回应窗口
    'recognizable_target',     # 批次含可识别目标
    'frequency_budget',        # 频率预算已攒够，进入一次候选
    'reply_necessity',         # 回复必要性评分达到阈值
})

# Bot 上一条回复后的自然跟进窗口。只在这一短时限内允许把普通群消息抬入
# DELIBERATE；超过后必须重新出现真信号或攒够扩展触发预算，避免一次发言
# 让后续十分钟的群噪声全部进入意识。
NATURAL_REPLY_WINDOW_MS = 90_000

# 「Bot 正在参与的话题还在继续」的消息距离口径：从 Bot 上一条回复起（含那条）算，
# 群里累计不超过这么多条消息时视为话题没有走远。
#
# 与 NATURAL_REPLY_WINDOW_MS 量纲不同且互不覆盖，两条都需要：
# - 活跃群聊数秒内可产生十余条消息，话题早已切换，只有时限口径能拦截；
# - 冷清群聊三分钟内仅两三条消息，话题未变，只有条数口径能覆盖。
ONGOING_TOPIC_MESSAGE_SPAN = 6

# 戳一戳的信号窗口与次数：同一 stream 在窗口内最多有这么多次戳一戳能唤起回合，
# 超出的戳一戳在收集注意力信号之前直接丢弃。不复用回复频率窗口，因为两者
# 统计的事件、时间尺度和用户意图都不同。
#
# - 现象：窗口 60 秒、上限 4 次时该上限从未触发。真机一段连戳的到达间隔为三十秒
#   到一分多钟，60 秒窗口内最多只攒到 3 次，单日 13 次到达全部进入意识并逐条回复。
# - 原因：poked_me 命中 direct_poke 抬入码，戳一戳必然进入 DELIBERATE；即便把
#   direct_poke 从抬入码中去掉也无效——Bot 刚回过上一次，紧随其后的戳一戳会被
#   natural_reply_window 命中。只有排在信号收集之前的丢弃才有效。
# - 后果：窗口需覆盖真实的连戳节奏，因此取 5 分钟；上限 3 表示同一段连戳最多唤起
#   三次回合。按上述 13 次到达回放，该组合唤起 7 次、丢弃 6 次。被丢弃的戳一戳仍
#   按静默消息落库并进入历史，Bot 下一轮可见对方戳了多次。
POKE_SIGNAL_WINDOW_MS = 300_000
POKE_SIGNAL_LIMIT = 3

_DISPOSITION_CODE_SETS: dict[GateDisposition, frozenset[str]] = {
    'drop': DROP_GATE_CODES,
    'force': FORCE_GATE_CODES,
    'deliberate': DELIBERATE_GATE_CODES,
}


@dataclass(frozen=True)
class GateRequest:
    """门控所需的确定性输入，全部由运行时按事实填充。

    名字/别名匹配（name_mentioned）由调用方使用配置中的名称集合计算后传入，
    门控自身不包含任何写死的称呼。
    """

    stream_kind: StreamKind
    mentioned_me: bool
    name_mentioned: bool
    asleep: bool
    at_mention_must_reply: bool
    replies_in_window: int
    max_replies_in_window: int
    is_self_message: bool = False
    # 本批是否包含「有人戳了 Bot」。它是明确指名的直接互动，但不作 FORCE：
    # 戳一戳不带任何内容，强制回复会在连续戳一戳时产生大量无内容回复；动作集
    # 中已有 poke 可作回应。因此只抬入 DELIBERATE，是否回应由 Bot 决定。
    poked_me: bool = False
    # 同一 stream 在信号窗口内的 poke 到达数，包含当前这一次；普通消息恒为 0。
    # 超过 POKE_SIGNAL_LIMIT 的到达不再唤起回合，由 poke_repeat 丢弃。
    pokes_in_window: int = 0
    # 本批是否包含「有人给 Bot 的消息贴了表情回应」。与戳一戳同口径只抬入
    # DELIBERATE：它是明确的社交反馈，但群里贴表情非常频繁，FORCE 会造成大量无内容回复；
    # Bot 自己的动作集里有 react，是否回应由 Bot 自己决定。
    emoji_liked_me: bool = False
    reply_to_bot: bool = False
    pending_thread_available: bool = False
    # Bot 正在参与的话题是否仍在继续。调用方按消息距离填充：从 Bot 上一条回复起
    # （含那条）群里累计消息不超过 ONGOING_TOPIC_MESSAGE_SPAN 条即为真。
    current_topic_available: bool = False
    is_clear_question: bool = False
    recognizable_target: bool = False
    candidate_message_ids: tuple[int, ...] = ()
    # 距 Bot 上一条回复的毫秒数；由调用方从持久化消息时间戳计算。None 表示尚无回复。
    last_bot_reply_elapsed_ms: int | None = None
    # 上一次自然跟进机会里 Bot 是否主动选择了沉默。由调用方按 Agent 的终局动作维护：
    # 群聊里一旦选择 silent 即置真，一旦成功回复即置假。它是自然回应窗口的关闭条件，
    # 表示 Bot 已看过该轮且未回应这一事实，而非任何计数。
    follow_up_declined: bool = False

    def __post_init__(self) -> None:
        """拒绝负计数、零窗口上限与负回复间隔，防止频率比较被错误输入翻转。"""
        if self.replies_in_window < 0:
            raise ValueError('窗口内回复数不能为负')
        if self.pokes_in_window < 0:
            raise ValueError('窗口内戳一戳次数不能为负')
        if self.max_replies_in_window < 1:
            raise ValueError('窗口回复上限必须大于 0')
        if self.last_bot_reply_elapsed_ms is not None and self.last_bot_reply_elapsed_ms < 0:
            raise ValueError('距上一条 Bot 回复的毫秒数不能为负')


@dataclass(frozen=True)
class GateResult:
    """三态门控结果：门控态与封闭原因码，可整体序列化进审计事件。"""

    disposition: GateDisposition
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        """拒绝未知门控态与不属于该态的原因码，保证枚举封闭。"""
        if self.disposition not in ('drop', 'force', 'deliberate'):
            raise ValueError(f'未知门控态：{self.disposition}')
        if not self.reason_codes:
            raise ValueError('门控原因码不能为空')
        allowed = _DISPOSITION_CODE_SETS[self.disposition]
        for code in self.reason_codes:
            if code not in allowed:
                raise ValueError(f'门控原因码 {code} 不属于 {self.disposition}')

    def as_trace(self) -> dict[str, Any]:
        """转换为 trace 使用的可序列化字典。

        :return: 含门控态与原因码列表的驼峰字段字典。
        """
        return {
            'disposition': self.disposition,
            'reasonCodes': list(self.reason_codes),
        }


def mentions_bot_name(text: str, bot_names: Sequence[str]) -> bool:
    """判断正文是否包含配置声明的主体名称或别名。

    :param text: 待匹配的消息正文。
    :param bot_names: 可用名称序列；比较时忽略大小写。空字符串条目被跳过：
        空名称配置不可能被任何正文匹配，跳过是名称匹配的合法语义而非兜底。

    :return: 正文包含任一非空完整配置值时返回 True，否则返回 False。

    :raises TypeError: 输入元素不支持字符串操作时由 Python 直接抛出。
    """
    normalized_text = text.casefold()
    for raw_name in bot_names:
        name = raw_name.strip().casefold()
        if not name:
            continue
        # 名称可以由任意文字或符号组成；这里只比较配置值，不推断字符类别或语义边界。
        if name in normalized_text:
            return True
    return False


def decide_disposition(request: GateRequest) -> GateResult:
    """按硬边界与注意力信号计算一次候选批次的门控态。

    优先级从高到低：
    1. Bot 自己的消息直接 DROP，永不回环；
    2. 同一 stream 的戳一戳在信号窗口内超出上限时直接 DROP；
    3. 用户发起的 QQ 私聊与桌面交互是明确问答契约，FORCE 且不允许 silent；
    4. 群聊真实 @ 且 @必回开启时 FORCE（先于休眠与频率硬限）；
    5. 休眠 DROP；
    6. 频率窗口硬上限 DROP，但只在没有任何直接点名信号时生效：@、名字/别名、
       被戳、回复 Bot 的消息都不受该上限约束；
    7. 任一便宜注意力信号命中则 DELIBERATE，全部未命中则按注意力过滤 DROP。

    :param request: 已按事实填充的门控输入。
    :return: 携带门控态与原因码的 GateResult。
    :raises ValueError: 输入违反 GateRequest 约束时抛出。
    """
    if request.is_self_message:
        return GateResult('drop', ('self_message',))
    if request.poked_me and request.pokes_in_window > POKE_SIGNAL_LIMIT:
        return GateResult('drop', ('poke_repeat',))
    if request.stream_kind in ('desktop', 'direct'):
        return GateResult('force', ('direct_conversation',))
    if request.mentioned_me and request.at_mention_must_reply:
        return GateResult('force', ('at_mention_must_reply',))
    if request.asleep:
        return GateResult('drop', ('asleep',))
    # 直接冲着 Bot 来的信号先收齐：@、名字/别名、被戳、回复 Bot 的消息。
    codes: list[str] = []
    if request.mentioned_me:
        codes.append('direct_mention')
    if request.name_mentioned:
        codes.append('name_mention')
    if request.poked_me:
        codes.append('direct_poke')
    if request.emoji_liked_me:
        codes.append('direct_emoji_like')
    if request.reply_to_bot:
        codes.append('reply_to_bot')
    # 频率硬上限只约束无人点名的自发参与，不约束点名交互。
    #
    # 该上限此前排在全部注意力信号之前，只有真实 @ 且 @必回开启能越过：
    # - 现象：真机 6 小时内 49 次 rate_limited 丢弃，其中 3 条是有人直接叫 Bot 名字
    #   （「小璃你要为我做主啊」「小璃快跑」），Bot 均无反应。
    # - 原因：上限的判据是 Bot 自己已发言多少，与该消息是否点名 Bot 无关；
    #   排在信号之前会使频率判断压过点名信号。
    # - 后果：直接点名类信号不再被上限压掉，但仍然只抬入 DELIBERATE——Bot 可以选
    #   沉默；自发参与（自然窗口 / 话题延续 / 必要性评分）继续受上限约束，
    #   频率边界没有放宽。
    if not codes and request.replies_in_window >= request.max_replies_in_window:
        return GateResult('drop', ('rate_limited',))
    if request.pending_thread_available:
        codes.append('pending_thread')
    if request.is_clear_question:
        codes.append('clear_question')
    # Bot 发言之后的跟进有两条口径，任一命中即抬入 DELIBERATE，并共用同一个关闭
    # 条件：Bot 在上一次机会里放弃过。
    #
    # - natural_reply_window 针对紧随其后的话，按时限判定；
    # - ongoing_topic 针对间隔较久但话题未走远的话，按消息距离判定。
    #   真机上「肘，我们去收拾他」距 Bot 上一条回复 3 分 11 秒（超时限）、中间只隔了
    #   1 条消息（未超距离），属于只有条数口径能覆盖的情形。
    #
    # 该窗口曾以「十分钟窗口内这是第一条回复」（replies_in_window == 1）为关闭
    # 条件，用回复计数近似发言额度：
    # - 现象：Bot 刚发言后别人紧接着说的话被判为无信号群噪声。真机上出现过距上一条
    #   回复仅 5 秒、正在对 Bot 说话的三条消息连续落到回复必要性评分并被丢弃。
    # - 原因：计数在活跃群聊里迅速超过 1，窗口对当轮之后的全部消息永久关闭；
    #   而计数与该消息是否点名 Bot 没有任何因果关系。
    # - 后果：改用 Bot 自己的终局动作作为关闭条件——选择 silent 表示已看过且不回应，
    #   窗口关闭；成功回复表示对话仍在继续，窗口重新开启。窗口不无限自续：Bot 每次
    #   放弃后须重新被真信号或攒批唤醒；防轰炸的最终边界仍是
    #   max_replies_in_window 硬上限，不由本条件承担。
    if not request.follow_up_declined:
        if (
            request.last_bot_reply_elapsed_ms is not None
            and request.last_bot_reply_elapsed_ms <= NATURAL_REPLY_WINDOW_MS
        ):
            codes.append('natural_reply_window')
        if request.current_topic_available:
            codes.append('ongoing_topic')
    if request.recognizable_target:
        codes.append('recognizable_target')
    if not codes:
        return GateResult('drop', ('attention_filtered',))
    return GateResult('deliberate', tuple(codes))
