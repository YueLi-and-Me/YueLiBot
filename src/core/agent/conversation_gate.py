"""三态门控：DROP / FORCE / DELIBERATE。

确定门控只负责硬边界与注意力过滤，不参与「愿不愿意说」的社会判断。DROP
只处理确定、无语义争议的过滤（Bot 自己的消息、休眠、频率硬上限、无任何
注意力信号的群聊噪声）；FORCE 保证直接对话与 @必回必须回应且不允许
silent；DELIBERATE 把候选批次交给 Conversation Agent 自主选择。

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
    'asleep',                   # 休眠
    'rate_limited',             # 频率窗口硬上限
    'attention_filtered',       # 无任何注意力信号，不值得进入意识
    'consumed',                 # 已被消费的重复消息
    'timeout_window',           # 超出超时窗
    'message_type_disallowed',  # 配置禁止的消息类型
})

# FORCE 原因：直接对话契约或 @必回，模型没有 silent 选项。
FORCE_GATE_CODES: frozenset[str] = frozenset({
    'at_mention_must_reply',  # 真实 @ 且 @必回开启
    'direct_conversation',    # 私聊 / 桌面直接对话
    'system_confirmation',    # 明确的系统确认或用户操作结果
})

# DELIBERATE 原因：便宜注意力信号，只决定「进入意识」，不决定「回不回」。
DELIBERATE_GATE_CODES: frozenset[str] = frozenset({
    'name_mention',            # 出现名字/别名但无真实 @
    'direct_mention',          # 真实 @ 但 @必回 未开启
    'reply_to_bot',            # 回复了 Bot 的消息
    'ongoing_topic',           # Bot 正在参与的话题在继续
    'pending_thread',          # 存在当前人物的未决线索
    'clear_question',          # 明确问题
    'natural_reply_window',    # Bot 最近发言后的自然回应窗口
    'recognizable_target',     # 批次含可识别目标
})

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
    reply_to_bot: bool = False
    pending_thread_available: bool = False
    current_topic_available: bool = False
    is_clear_question: bool = False
    recognizable_target: bool = False
    candidate_message_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        """拒绝负计数与零窗口上限，防止频率比较被错误输入翻转。"""
        if self.replies_in_window < 0:
            raise ValueError('窗口内回复数不能为负')
        if self.max_replies_in_window < 1:
            raise ValueError('窗口回复上限必须大于 0')


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
    2. 私聊与桌面是直接对话契约，FORCE 且不允许 silent；
    3. 群聊真实 @ 且 @必回开启时 FORCE（先于休眠与频率硬限）；
    4. 休眠、频率窗口硬上限依次 DROP；
    5. 任一便宜注意力信号命中则 DELIBERATE，全部未命中则按注意力过滤 DROP。

    :param request: 已按事实填充的门控输入。
    :return: 携带门控态与原因码的 GateResult。
    :raises ValueError: 输入违反 GateRequest 约束时抛出。
    """
    if request.is_self_message:
        return GateResult('drop', ('self_message',))
    if request.stream_kind in ('desktop', 'direct'):
        return GateResult('force', ('direct_conversation',))
    if request.mentioned_me and request.at_mention_must_reply:
        return GateResult('force', ('at_mention_must_reply',))
    if request.asleep:
        return GateResult('drop', ('asleep',))
    if request.replies_in_window >= request.max_replies_in_window:
        return GateResult('drop', ('rate_limited',))
    codes: list[str] = []
    if request.mentioned_me:
        codes.append('direct_mention')
    if request.name_mentioned:
        codes.append('name_mention')
    if request.reply_to_bot:
        codes.append('reply_to_bot')
    if request.pending_thread_available:
        codes.append('pending_thread')
    if request.current_topic_available:
        codes.append('ongoing_topic')
    if request.is_clear_question:
        codes.append('clear_question')
    # Bot 最近在窗口内说过话，自然回应窗口仍然敞开。
    if request.replies_in_window > 0:
        codes.append('natural_reply_window')
    if request.recognizable_target:
        codes.append('recognizable_target')
    if not codes:
        return GateResult('drop', ('attention_filtered',))
    return GateResult('deliberate', tuple(codes))
