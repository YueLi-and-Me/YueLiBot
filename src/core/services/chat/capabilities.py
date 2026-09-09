"""平台能力与动作可用性。

本 mixin 把「配置开关」与「协议端实测能力」合成一个判据，决定表情包、表情
回应与戳一戳这三个动作在当前出口是否可选，并维护戳一戳到达的信号窗口。

判据必须是两者的与：只看配置开关会让她反复选中一个执行不了的终局动作，
对方收到的是彻底的沉默。未收到能力上报时按不可用处理。

由 ``ChatService`` 继承，依赖它的配置与会话状态属性。
"""

from collections import deque
from typing import Iterable

from src.core.agent.conversation_gate import POKE_SIGNAL_WINDOW_MS
from src.core.platform_io.types import ConversationContext
from src.core.runtime.clock import now as current_time

from .constants import EMOJI_MAX_PER_REPLY_WINDOW


class PlatformCapabilityMixin:

    def record_poke_arrival(self, stream_id: int, arrived_at: int) -> int:
        """登记一次 poke 到达并返回当前信号窗口内的次数。

        :param stream_id: poke 所属的稳定 stream 主键。
        :param arrived_at: 本次入站的 Unix 毫秒时间戳。
        :return: 清理过期项并包含本次到达后的窗口计数。
        副作用：更新进程内短窗口队列；不写消息、事件或配置。
        """
        arrivals = self._poke_arrivals.setdefault(stream_id, deque())
        window_start = arrived_at - POKE_SIGNAL_WINDOW_MS
        while arrivals and arrivals[0] < window_start:
            arrivals.popleft()
        arrivals.append(arrived_at)
        return len(arrivals)

    def _emoji_available(self, context: ConversationContext) -> bool:
        """判断当前 QQ stream 是否仍有表情包库和窗口发送额度。"""

        if (
            context.stream.platform != 'qq'
            or self._emoji_library is None
            or not self._emoji_library.has_sendable()
        ):
            return False
        since = current_time() - self._cfg.group_chat.reply_window_minutes * 60_000
        return (
            self.memory.emoji_reply_count_since(context.stream.id, since)
            < EMOJI_MAX_PER_REPLY_WINDOW
        )

    def _react_available(self, context: ConversationContext) -> bool:
        """判断当前 stream 能否执行 QQ 表情回应。

        三个条件缺一不可：平台是 QQ（只有它有这个协议动作）、会话是群聊（表情回应
        的可见性只在群聊有意义）、以及配置显式开启。

        开关的取值语义见 ``GroupChatConfig.reactions_enabled``：语义反应名到 QQ
        表情编号的映射（``napcat/segments.py`` 的 ``REACTION_EMOJI_IDS``）已按
        协议端表情编号表逐条核对，排除了名称正确但编号错误且不报错的情况。

        不设独立频率预算：贴表情的前提是本轮已取得候选机会，已受门控与
        ``max_replies_in_window`` 约束；额外窗口常量会引入互相牵制的参数。

        :param context: 当前会话上下文。
        :return: 允许 react 进入动作集时返回 ``True``。
        """
        return (
            context.stream.platform == 'qq'
            and context.stream.kind == 'group'
            and self._cfg.group_chat.reactions_enabled
            and self._backend_supports(context, 'reaction')
        )

    def _poke_available(self, context: ConversationContext) -> bool:
        """判断当前 stream 能否戳一戳。

        条件与表情回应同构（QQ + 群聊 + 配置开关 + 协议端能力），但默认关闭：
        表情回应无推送，戳一戳会给对方推送提醒，扰动量级不同，须由使用者主动打开。

        :param context: 当前会话上下文。
        :return: 允许 poke 进入动作集时返回 ``True``。
        """
        return (
            context.stream.platform == 'qq'
            and context.stream.kind == 'group'
            and self._cfg.group_chat.pokes_enabled
            and self._backend_supports(context, 'poke')
        )

    def set_platform_capabilities(
        self,
        platform: str,
        capabilities: Iterable[str],
    ) -> None:
        """登记某个平台的协议端当前实际具备的能力。

        每次适配器连接成功都会重新上报一次，因此这里是整体替换而不是并入：
        协议端的能力会随其自身状态变化（例如发包组件与客户端版本不匹配时戳一戳
        整体失效），保留上一次连接的结论会让已经失效的动作继续进入动作集。

        :param platform: 平台标识，例如 ``qq``。
        :param capabilities: 该平台协议端实测可用的能力名。
        :return: ``None``。
        副作用：替换该平台的能力集合，影响后续回合的动作集。
        """
        self._platform_capabilities[platform] = frozenset(capabilities)

    def _backend_supports(self, context: ConversationContext, capability: str) -> bool:
        """判断该会话所在平台的协议端是否具备某项能力。

        未收到过上报时一律返回 ``False``。方向是刻意的：未知按不可用处理，最坏
        结果是她少用一个动作；反过来按可用处理，她会选中一个执行不了的终局动作，
        对方收到的是彻底的沉默，而账本里查不出来。

        :param context: 当前会话上下文。
        :param capability: 能力名，取值见 ``src.plugin_system.capabilities``。
        :return: 该平台已上报且包含该能力时返回 ``True``。
        """
        return capability in self._platform_capabilities.get(
            context.stream.platform, frozenset(),
        )
