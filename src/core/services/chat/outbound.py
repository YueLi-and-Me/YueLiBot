"""解析事件的消费与出站投递。

本 mixin 把解析器产出的事件落成实际动作：心情与承诺等副作用写库、台词按打字
节奏切成气泡逐条投递、引用目标解析、以及非桌面平台的出站发送与节奏延时。

切分放在这里而不是投递侧：平台出站、助手历史与控制台渲染共用同一份 segments，
切分前置才能保证三者看到的气泡完全一致。

由 ``ChatService`` 继承，依赖它的 ``_broker`` / ``_memory`` / ``_persona`` 等属性。
"""

import asyncio
import inspect

from src.core.agent.parser import (
    MoodEvent,
    ParseEvent,
    PromiseEvent,
    SayEndEvent,
    SayEvent,
    TextEvent,
)
from src.core.agent.segmentation import typing_delay_seconds
from src.core.logging.logger import get_logger
from src.core.observe import events as trace
from src.core.observe.stages import DISPATCHING
from src.core.persona.state import EventDelta
from src.core.platform_io.types import ConversationContext, OutboundMessage

logger = get_logger(__name__)


class OutboundDispatchMixin:

    def _handle_side_effects(
        self, context: ConversationContext, event: ParseEvent, now: int, turn: int,
        sink: list[dict] | None = None,
        source_text: str | None = None,
    ) -> None:
        """应用单个解析事件携带的事实、情绪或约定副作用。

        :param context: 当前会话上下文。
        :param event: 解析器产生的事件。
        :param now: 当前回合的毫秒时间戳。
        :param turn: 当前对话回合 ID。
        :param sink: 可选的面板副作用列表。
        :param source_text: 当前用户原话；promise 事件必须提供。

        :raises ValueError: promise 事件缺少用户原话。
        :raises Exception: 记忆、人格或 promise 回调写入失败时直接传播。

        副作用：
            可能写入事实、人格状态或待投放 promise，并发出对应观察事件。
        """

        if isinstance(event, MoodEvent):
            # 群聊关系增量由上下文决定权重，Persona 本身不感知平台会话。
            self.persona.apply_event(
                context.person.id,
                EventDelta(favor=event.favor, energy=event.energy),
                now,
                weight=self._persona_weight(context),
            )
            trace.emit('mood_delta', turnId=turn, favor=event.favor, energy=event.energy)
            if sink is not None:
                sink.append({'kind': 'mood_delta', 'favor': event.favor, 'energy': event.energy})
        elif isinstance(event, PromiseEvent):
            # promise 只允许 owner 关系信号进入主动调度，联系人消息不能改变主体计划。
            if not context.relationship_signals_enabled:
                logger.warning(
                    'promise_rejected_for_person',
                    turnId=turn,
                    personKind=context.person.kind,
                )
                return
            if self._promise_handler is None:
                logger.warning('promise_handler_missing', turnId=turn)
                return
            if source_text is None:
                raise ValueError('约定事件必须关联本轮用户原话')
            self._promise_handler(event.at, source_text)
            trace.emit('promise_stashed', turnId=turn, at=event.at, subject=source_text)
            if sink is not None:
                sink.append({'kind': 'promise_stashed', 'at': event.at, 'subject': source_text})

    def _dispatch_speech(self, context: ConversationContext, text: str, turn: int) -> None:
        """将一条桌面 ``<say>`` 分句交给语音回调。

        :param context: 当前会话上下文。
        :param text: 待合成的分句文本。
        :param turn: 关联的对话回合 ID。

        副作用：
            可能调用同步或异步语音回调；回调异常只记录警告，不影响文本消息流程。
        """
        if context.stream.kind != 'desktop' or not self._speak_audio:
            return
        line = text.strip()
        if not line:
            return
        try:
            result = self._speak_audio(line, turn)
            # TtsService.speak 已自行创建任务，其他注入实现可能返回协程，因此分别处理两种返回形式。
            if inspect.isawaitable(result):
                asyncio.create_task(result)
        except Exception as exc:
            logger.warning('speak_audio_failed', turnId=turn, error=str(exc))

    def _track_speech(self, context: ConversationContext, event: ParseEvent, turn: int) -> None:
        """在流式解析过程中聚合一条完整 ``<say>`` 分句并触发合成。

        :param context: 当前会话上下文。
        :param event: 当前解析事件。
        :param turn: 关联的对话回合 ID。

        副作用：
            更新指定 stream 的台词缓冲；收到 ``SayEndEvent`` 时调用语音回调。
            每个分句独立发送，以便音频与桌面解析事件保持顺序。
        """
        if not self._speak_audio:
            return
        stream_id = context.stream.id
        if isinstance(event, SayEvent):
            self._speech_buffer[stream_id] = []
        elif isinstance(event, TextEvent):
            self._speech_buffer.setdefault(stream_id, []).append(event.value)
        elif isinstance(event, SayEndEvent):
            line = ''.join(self._speech_buffer.pop(stream_id, []))
            self._dispatch_speech(context, line, turn)

    async def _emit_parse_event(
        self,
        context: ConversationContext,
        turn: int,
        event: ParseEvent,
    ) -> None:
        """将解析事件转换为桌面端 ``chat.event`` 载荷。

        :param context: 当前会话上下文。
        :param turn: 对话回合 ID。
        :param event: 解析器产生的事件。

        副作用：
            仅在 desktop stream 上通过 ``_emit`` 推送一个解析事件；未知事件类型
            不产生输出。
        """

        if context.stream.platform != 'desktop':
            return
        # 仅传输前端可渲染的事件字段，内部对象和未定义事件不越过平台边界。
        if isinstance(event, SayEvent):
            ev = {'turnId': turn, 'kind': 'parse',
                  'event': {'type': 'say', **({'emotion': event.emotion} if event.emotion else {}),
                             **({'gesture': event.gesture} if event.gesture else {})}}
        elif isinstance(event, TextEvent):
            ev = {'turnId': turn, 'kind': 'parse', 'event': {'type': 'text', 'value': event.value}}
        elif isinstance(event, SayEndEvent):
            ev = {'turnId': turn, 'kind': 'parse', 'event': {'type': 'sayEnd'}}
        elif isinstance(event, MoodEvent):
            ev = {'turnId': turn, 'kind': 'parse',
                  'event': {'type': 'mood',
                             **({'favor': event.favor} if event.favor is not None else {}),
                             **({'energy': event.energy} if event.energy is not None else {})}}
        else:
            return
        await self._emit(context.stream.id, 'chat.event', ev)

    def _quote_target(
        self,
        context: ConversationContext,
        target_message_ids: tuple[int, ...],
    ) -> str | None:
        """判断这一轮回复是否需要挂引用，并给出被引用消息的平台编号。

        群聊消息滚动快，一轮生成期间会有新消息落在目标之后，回复落地时旁观者
        无法确定回应对象。因此判据只有一条：目标消息之后本 stream 已经出现
        更新的消息，就挂引用。私聊只有两方，不存在指认歧义，一律不引用。

        引用与否不进模型的动作头：目标由模型选定，引用是该选择在平台上的呈现
        方式，由代码强制；增加模型可见字段会增加出错面。

        :param context: 目标会话上下文。
        :param target_message_ids: 决策选中的目标消息内部 ID；为空表示无目标。
        :return: 被引用消息的平台编号；不需要或无法引用时返回 ``None``。
        """
        if context.stream.kind != 'group' or not target_message_ids:
            return None
        target_id = target_message_ids[0]
        if not self.memory.has_user_messages_after(context.stream.id, target_id):
            return None
        # 平台编号缺失说明这条消息早于编号落库改动，或来自不带编号的通道，
        # 此时只能不引用；不得以内部 ID 冒充平台编号投递。
        return self.memory.external_message_id(context.stream.id, target_id)

    async def _dispatch_outbound(
        self,
        context: ConversationContext,
        turn: int,
        segments: list[str],
        emoji_items: list[tuple[str, str, int]],
        quote_external_message_id: str | None = None,
    ) -> None:
        """将非桌面整轮回复交给平台 broker。

        :param context: 目标会话上下文。
        :param turn: 对话回合 ID。
        :param segments: 已按 ``<say>`` 边界切分的正文列表。
        :param emoji_items: 已按目标情绪命中的可发送表情包引用。
        :param quote_external_message_id: 第一条气泡要引用的平台消息编号；
            ``None`` 表示不引用。

        副作用：
            可能调用平台驱动并写入投递观察事件；空列表只记录警告并返回。

        :raises RuntimeError: 桌面 stream 误走 broker，或非桌面 stream 未配置 broker。
        :raises DeliveryError: 平台驱动未注册或投递失败时由 broker 传播。
        """

        self._mark_stage(context, DISPATCHING, turn_id=turn)
        if context.stream.platform == 'desktop':
            raise RuntimeError('desktop stream 不能经由非桌面 broker 投递')
        if self._broker is None:
            raise RuntimeError('非桌面 stream 未配置 PlatformBroker')
        if not segments and not emoji_items:
            logger.warning('outbound_reply_empty', streamId=context.stream.id, turnId=turn)
            return
        # Broker 负责平台驱动选择和失败归一化；图片引用由 QQ 驱动透传给适配器。
        emoji_refs = tuple(reference for _emotion, reference, _sub_type in emoji_items)
        receipt = await self._broker.dispatch(OutboundMessage(
            stream=context.stream,
            segments=segments,
            emoji_refs=emoji_refs,
            emoji_sub_types=tuple(sub_type for _emotion, _reference, sub_type in emoji_items),
            batch_delays_ms=self._batch_delays_ms(segments, len(emoji_items)),
            quote_external_message_id=quote_external_message_id,
            turn_id=turn,
        ))
        # 只有拿到投递回执（发送成功）才回写使用记录；发送失败不动两列，
        # 淘汰判据不允许把「没发出去」记成「用过」。
        if self._emoji_library is not None:
            for reference in emoji_refs:
                self._emoji_library.record_use(reference)
        trace.emit(
            'outbound_delivered',
            platform=receipt.platform,
            streamId=receipt.stream_id,
            turnId=turn,
        )

    def _batch_delays_ms(self, segments: list[str], emoji_count: int) -> tuple[int, ...]:
        """按人格配置算出每个发送批次发出前的停顿。

        节奏在主体侧算好随出站载荷下发，适配器只负责照做：打字速度是角色行为
        参数，不该散落到各平台适配器里各算一套。

        :param segments: 已按打字习惯切分的气泡文本。
        :param emoji_count: 排在文字之后的表情包张数。
        :return: 与「每条文字一批、每张表情包一批」逐项对齐的毫秒停顿；首项恒为
            0，因为模型生成本身已经占用了十几秒，Bot 在对方视角里早就在打字了。
        """
        typing = self._cfg.typing
        text_delays = [
            0 if index == 0 else int(typing_delay_seconds(segment, typing) * 1000)
            for index, segment in enumerate(segments)
        ]
        # 表情包不逐字打，用固定的挑图时间；整轮只有表情包时同样不等第一条。
        emoji_delay = int(typing.emoji_pick_seconds * 1000) if typing.delay_enabled else 0
        emoji_delays = [emoji_delay] * emoji_count
        delays = text_delays + emoji_delays
        if delays:
            delays[0] = 0
        return tuple(delays)
