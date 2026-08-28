"""三态门控：DROP / FORCE / DELIBERATE。

确定门控只负责硬边界与注意力过滤，不参与「愿不愿意说」的社会判断。DROP
只处理确定、无语义争议的过滤（Bot 自己的消息、休眠、频率硬上限、无任何
注意力信号的群聊噪声）；FORCE 保证用户发起的私聊、桌面交互与 @必回必须
回应且不允许 silent；DELIBERATE 把普通群候选交给 Conversation Agent 自主选择。

便宜候选信号只决定「是否进入意识」，不决定「回不回」：名字/别名、回复
Bot、未决线索、正在进行的话题、明确问题、自然回应窗口等信号把普通群聊
批次抬入 DELIBERATE，其余纯噪声在批次成形前即被 DROP。名字与别名全部
由调用方从 Bot 配置动态提供（通过 name_mentioned 事实传入），本模块
不读取配置、不写死任何称呼。

依赖：src.core.agent.action_protocol 的 GateDisposition 与
src.core.platform_io.types 的 StreamKind；被平台入口与聊天
编排服务调用，不访问数据库、不调用模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .action_protocol import GateDisposition

from src.core.platform_io.types import StreamKind

# DROP 原因：全部是确定、无语义争议的硬过滤，不承载「她大概不想说」。
DROP_GATE_CODES: frozenset[str] = frozenset({
    'self_message',             # Bot 自己发的消息
    'poke_flood',               # 同一 stream 在短窗口内被连续戳动
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

# DELIBERATE 原因：便宜注意力信号，只决定「进入意识」，不决定「回不回」。
DELIBERATE_GATE_CODES: frozenset[str] = frozenset({
    'name_mention',            # 出现名字/别名但无真实 @
    'direct_mention',          # 真实 @ 但 @必回 未开启
    'direct_poke',             # 有人戳了 Bot 自己
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

# 「她正在参与的话题还在继续」的消息距离口径：从她上一条回复起（含那条）算，
# 群里累计不超过这么多条消息时视为话题没有走远。
#
# 与 NATURAL_REPLY_WINDOW_MS 量纲不同且互不覆盖，两条都需要：
# - 群热时几秒内就能刷过十几条，话题早换了，只有时限收得住；
# - 群温吞时三分钟才两句，话题一点没变，只有条数接得住。
ONGOING_TOPIC_MESSAGE_SPAN = 6

# 戳一戳洪泛只使用这一组窗口与次数：同一 stream 的第 4 次及以后直接丢弃。
# 它不复用回复频率窗口，因为两者统计的事件、时间尺度和用户意图都不同。
POKE_FLOOD_WINDOW_MS = 60_000
POKE_FLOOD_LIMIT = 4

_DISPOSITION_CODE_SETS: dict[GateDisposition, frozenset[str]] = {
    'drop': DROP_GATE_CODES,
    'force': FORCE_GATE_CODES,
    'deliberate': DELIBERATE_GATE_CODES,
}


@dataclass(frozen=True)
class GateRequest:
    """门控所需的确定性输入，全部由运行时按事实填充。

    名字/别名匹配（name_mentioned）由调用方使用配置中的名称集合
    计算后传入，保证门控自身不接触任何写死的称呼。
    """

    stream_kind: StreamKind
    mentioned_me: bool
    name_mentioned: bool
    asleep: bool
    at_mention_must_reply: bool
    replies_in_window: int
    max_replies_in_window: int
    is_self_message: bool = False
    # 本批是否包含「有人戳了 Bot」。它是明确指名的直接互动，但**不作 FORCE**：
    # 戳一戳不带任何内容，强制回复会被连戳刷屏，而她的动作集里本来就有 poke，
    # 可以戳回去。因此只抬入 DELIBERATE，接不接由她自己决定。
    poked_me: bool = False
    # 同一 stream 在洪泛窗口内的 poke 到达数，包含当前这一次；普通消息恒为 0。
    pokes_in_window: int = 0
    # 本批是否包含「有人给 Bot 的消息贴了表情回应」。与戳一戳同口径只抬入
    # DELIBERATE：它是明确的社交反馈，但群里贴表情非常频繁，FORCE 会被刷屏；
    # 她自己的动作集里有 react，接不接由她自己决定。
    emoji_liked_me: bool = False
    reply_to_bot: bool = False
    pending_thread_available: bool = False
    # 她正在参与的话题是否仍在继续。调用方按消息距离填充：从她上一条回复起
    # （含那条）群里累计消息不超过 ONGOING_TOPIC_MESSAGE_SPAN 条即为真。
    current_topic_available: bool = False
    is_clear_question: bool = False
    recognizable_target: bool = False
    candidate_message_ids: tuple[int, ...] = ()
    # 距 Bot 上一条回复的毫秒数；由调用方从持久化消息时间戳计算。None 表示尚无回复。
    last_bot_reply_elapsed_ms: int | None = None
    # 上一次自然跟进机会里她是否主动选择了沉默。由调用方按 Agent 的终局动作维护：
    # 群聊里一旦选择 silent 即置真，一旦成功回复即置假。它是自然回应窗口的关闭条件，
    # 表达的是「她看过这一轮并决定不接」这一事实，而非任何计数。
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
    2. 同一 stream 的 poke 达到洪泛上限时直接 DROP；
    3. 用户发起的 QQ 私聊与桌面交互是明确问答契约，FORCE 且不允许 silent；
    4. 群聊真实 @ 且 @必回开启时 FORCE（先于休眠与频率硬限）；
    5. 休眠 DROP；
    6. 频率窗口硬上限 DROP，但**只在没有任何直接点名信号时生效**：@、名字/别名、
       被戳、回复她的消息都不受该上限约束；
    7. 任一便宜注意力信号命中则 DELIBERATE，全部未命中则按注意力过滤 DROP。

    :param request: 已按事实填充的门控输入。
    :return: 携带门控态与原因码的 GateResult。
    :raises ValueError: 输入违反 GateRequest 约束时抛出。
    """
    if request.is_self_message:
        return GateResult('drop', ('self_message',))
    if request.poked_me and request.pokes_in_window >= POKE_FLOOD_LIMIT:
        return GateResult('drop', ('poke_flood',))
    if request.stream_kind in ('desktop', 'direct'):
        return GateResult('force', ('direct_conversation',))
    if request.mentioned_me and request.at_mention_must_reply:
        return GateResult('force', ('at_mention_must_reply',))
    if request.asleep:
        return GateResult('drop', ('asleep',))
    # 直接冲着她来的信号先收齐：@、名字/别名、被戳、回复她的消息。
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
    # 频率硬上限只约束「没人点名的自发参与」，不约束「有人正在叫她」。
    #
    # 该上限此前排在全部注意力信号之前，只有真实 @ 且 @必回开启能越过：
    # - 现象：真机 6 小时内 49 次 rate_limited 丢弃，其中 3 条是有人直接叫她名字
    #   （「小璃你要为我做主啊」「小璃快跑」），她因为「说太多了」完全没有反应。
    # - 原因：上限的判据是她自己说了多少，与「这句话是不是冲着她来的」无关；
    #   把它放在信号之前，等于让「我说够了」压过「有人在叫我」。
    # - 后果：直接点名类信号不再被上限压掉，但仍然只抬入 DELIBERATE——她可以选
    #   沉默；自发参与（自然窗口 / 话题延续 / 必要性评分）继续受上限约束，
    #   刷屏边界没有放宽。
    if not codes and request.replies_in_window >= request.max_replies_in_window:
        return GateResult('drop', ('rate_limited',))
    if request.pending_thread_available:
        codes.append('pending_thread')
    if request.is_clear_question:
        codes.append('clear_question')
    # 她开口之后的跟进有两条口径，任一命中即抬入 DELIBERATE，并共用同一个关闭
    # 条件：她在上一次机会里放弃过。
    #
    # - natural_reply_window 接的是「紧随其后的话」，按时限判定；
    # - ongoing_topic 接的是「冷了一会儿但话题没走远的话」，按消息距离判定。
    #   真机上「肘，我们去收拾他」距她上一条回复 3 分 11 秒（超时限）、中间只隔了
    #   1 条消息（未超距离），正是只有后者接得住的那一类。
    #
    # 该窗口曾以「十分钟窗口内这是第一条回复」（replies_in_window == 1）为关闭
    # 条件，用回复计数近似「她已经说够了」：
    # - 现象：她刚发言后别人紧接着说的话被判为无信号群噪声。真机上出现过距上一条
    #   回复仅 5 秒、正在对她说话的三条消息连续落到回复必要性评分并被丢弃。
    # - 原因：计数在活跃群聊里迅速超过 1，窗口对当轮之后的全部消息永久关闭；
    #   而计数与「这轮话是不是冲着她来的」没有任何因果关系。
    # - 后果：改用她自己的终局动作作为关闭条件——选择 silent 表示看过并决定不接，
    #   窗口关闭；成功回复表示对话仍在她这边，窗口重新敞开。窗口不再自我续期到
    #   无限，是因为她每放弃一次就要重新被真信号或攒批唤醒；防刷屏的最终边界仍是
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
