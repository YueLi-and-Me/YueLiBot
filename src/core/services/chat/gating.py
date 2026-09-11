"""批次门控与触发口径。

本 mixin 决定「这一批合并消息该不该开口」：把睡眠状态、名字命中、@ 事实与
回复必要性打分汇成三态门控结果，并给出群聊扩展触发与触发模式的读取口径。

由 ``ChatService`` 继承，依赖它的配置与会话状态属性。
"""

from src.core.agent.conversation_gate import (
    GateRequest,
    GateResult,
    decide_disposition,
    mentions_bot_name,
)
from src.core.agent.reply_necessity import (
    PRESENCE_WINDOW_MS,
    frequency_trigger_threshold,
    score_reply_necessity,
)
from src.core.platform_io.types import ConversationContext
from src.core.runtime.clock import now as current_time

from .state import _BatchGate


class BatchGateMixin:

    def _batch_gate(
        self,
        context: ConversationContext,
        batch_text: str,
        mentioned_me: bool,
        candidate_count: int = 1,
        *,
        poked_me: bool = False,
        pokes_in_window: int = 0,
        name_match_text: str | None = None,
    ) -> _BatchGate:
        """按本批合并事实重算三态门控；只读取确定性输入，不调用模型。

        戳一戳的三项事实全部由入口门控算好后随消息传入，本方法不重算：窗口计数
        在入口按每次到达登记，重算等于重复记账；合成正文的名字匹配已在入口排除，
        重算会使被排除的合成点名重新命中。

        :param context: 本批消息的会话上下文。
        :param batch_text: 合并后的本批正文。
        :param mentioned_me: 本批是否包含协议 @。
        :param candidate_count: 本批候选消息数；扩展触发模式用它累计频率预算。
        :param poked_me: 本批是否包含入口判定为有效信号的戳一戳。
        :param pokes_in_window: 本批戳一戳在入口信号窗口内的到达序号；无戳一戳时为 0。
        :param name_match_text: 参与名字匹配的正文；``None`` 表示与 ``batch_text``
            相同。批次含戳一戳时由调用方剔除合成正文后传入。
        :return: 门控结果与全部判定输入事实。
        """
        sleep = self.current_sleep()
        asleep = sleep.asleep
        reply_count = 0
        last_bot_reply_elapsed_ms: int | None = None
        current_topic_available = False
        if context.stream.kind == 'group':
            now = current_time()
            reply_count = self.memory.assistant_reply_count_since(
                context.stream.id,
                now - self._cfg.group_chat.reply_window_minutes * 60_000,
            )
            last_bot_reply_at = self.memory.last_assistant_reply_at(context.stream.id)
            if last_bot_reply_at is not None:
                last_bot_reply_elapsed_ms = now - last_bot_reply_at
                current_topic_available = self.topic_still_hers(
                    context.stream.id, last_bot_reply_at,
                )
        name_mentioned = (
            mentions_bot_name(
                batch_text if name_match_text is None else name_match_text,
                self._bot_names,
            )
            if context.stream.kind == 'group'
            else False
        )
        result = decide_disposition(GateRequest(
            stream_kind=context.stream.kind,
            mentioned_me=mentioned_me,
            name_mentioned=name_mentioned,
            sleep_level=sleep.level,
            at_mention_must_reply=self._at_mention_must_reply,
            replies_in_window=reply_count,
            max_replies_in_window=self._cfg.group_chat.max_replies_in_window,
            last_bot_reply_elapsed_ms=last_bot_reply_elapsed_ms,
            current_topic_available=current_topic_available,
            poked_me=poked_me,
            pokes_in_window=pokes_in_window,
            follow_up_declined=self.follow_up_declined(context.stream.id),
        ))
        plain_group_drop = (
            context.stream.kind == 'group'
            and result.disposition == 'drop'
            and result.reason_codes == ('attention_filtered',)
        )
        if plain_group_drop:
            if self.extended_trigger_enabled(context) and self._trigger_mode != 'signal':
                result = self._extended_group_gate(context, batch_text, candidate_count)
            else:
                # 扩展模式未接管时不留历史残留，避免切回扩展模式后旧计数
                # 造成立即触发。
                self._extended_pending.pop(context.stream.id, None)
        elif (
            context.stream.kind == 'group'
            and result.disposition in ('deliberate', 'force')
        ):
            # 只有真正获得候选机会的批次才清零扩展累计；deep_sleep、light_sleep、rate_limited
            # 等硬边界 DROP 不消费候选机会，保留之前的累计。
            self._extended_pending.pop(context.stream.id, None)
        return _BatchGate(
            result=result,
            asleep=asleep,
            name_mentioned=name_mentioned,
            reply_count=reply_count,
            mentioned_me=mentioned_me,
            last_bot_reply_elapsed_ms=last_bot_reply_elapsed_ms,
        )

    def _extended_group_gate(
        self,
        context: ConversationContext,
        batch_text: str,
        candidate_count: int,
    ) -> GateResult:
        """按配置的扩展口径决定无信号群消息是否进入 DELIBERATE。

        frequency 使用发言频率预算累计候选数；reply_necessity 以内容信号
        为主，并把累计候选数作为辅助压力项。两种模式都保留休眠、频率硬上限
        等上游边界，且都只产生确定性候选，不替 Agent 决定回复与否。

        :param context: 当前会话上下文。
        :param batch_text: 本批合并正文。
        :param candidate_count: 本批候选消息数。
        :return: 扩展门控产生的 drop 或 deliberate 结果。
        """
        stream_id = context.stream.id
        pending = self._extended_pending.get(stream_id, 0) + candidate_count
        if self._trigger_mode == 'frequency':
            threshold = frequency_trigger_threshold(self._frequency_talk_value)
            if pending >= threshold:
                self._extended_pending.pop(stream_id, None)
                return GateResult('deliberate', ('frequency_budget',))
            self._extended_pending[stream_id] = pending
            return GateResult('drop', ('frequency_wait',))
        if self._trigger_mode == 'reply_necessity':
            threshold = max(1, self._reply_necessity_threshold)
            # 压力分母必须是消息条数尺度；复用 frequency 预算折算阈值，
            # 不再把 0~100 的评分阈值当成条数使用。
            backlog_scale = frequency_trigger_threshold(self._frequency_talk_value)
            presence_since = current_time() - PRESENCE_WINDOW_MS
            score = score_reply_necessity(
                [batch_text],
                pending_count=pending,
                backlog_scale=backlog_scale,
                recent_self_replies=self.memory.assistant_reply_count_since(
                    stream_id,
                    presence_since,
                ),
                recent_window_messages=self.memory.message_count_since(
                    stream_id,
                    presence_since,
                ),
            )
            if score.score >= threshold:
                self._extended_pending.pop(stream_id, None)
                return GateResult('deliberate', ('reply_necessity',))
            self._extended_pending[stream_id] = pending
            return GateResult('drop', ('low_necessity',))
        return GateResult('drop', ('attention_filtered',))

    def _has_legacy_attention_signal(self, batch_gate: _BatchGate) -> bool:
        """判断本批是否携带旧 signal 口径下的可见注意力信号。

        shadow 阶段扩展触发模式可能单独产生 frequency_budget /
        reply_necessity 候选；这些候选在旧口径下会被 attention_filtered
        DROP，因此不应顺手唤醒旧管线改变可见行为。

        :param batch_gate: 本批门控快照。
        :return: 存在 @、名字提及或自然回应窗口时返回 True。
        """
        return (
            batch_gate.mentioned_me
            or batch_gate.name_mentioned
            or 'natural_reply_window' in batch_gate.result.reason_codes
        )

    def extended_trigger_enabled(self, context: ConversationContext) -> bool:
        """判断当前 stream 是否启用 frequency / reply_necessity 扩展口径。

        off 与 selected_streams 清单外的 stream 保持原 signal 行为，避免
        灰度观察之外的群聊被新触发模式改变候选边界。

        :param context: 当前消息的会话上下文。
        :return: 该 stream 可应用扩展触发模式时返回 True。
        """
        if self._conversation_mode == 'off' or self._conversation_agent is None:
            return False
        if self._conversation_mode in ('shadow', 'enabled'):
            return True
        return (
            self._conversation_mode == 'selected_streams'
            and context.stream.external_id in self._conversation_selected_streams
        )

    @property
    def conversation_trigger_mode(self) -> str:
        """返回当前配置的候选触发口径；仅供入口门控读取。"""
        return self._trigger_mode
