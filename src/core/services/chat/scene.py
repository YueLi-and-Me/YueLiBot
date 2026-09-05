"""场景观察的调度与呈现。

本 mixin 让观察 Agent 在后台读一段比工作记忆更长的历史，产出场景画像并转成
可注入提示词的文本行。窗口刻意宽于工作记忆：观察的价值就在于看到比单轮上下文
更长的一段，否则它只是把对话模型已经看过的东西再读一遍。

由 ``ChatService`` 继承，依赖它的 ``_memory`` 与观察 Agent 属性。
"""

import asyncio

from src.core.agent.history import strip_say_tags
from src.core.agent.observer import SceneSnapshot
from src.core.logging.logger import get_logger
from src.core.memory.store import StoredMessage
from src.core.observe import events as trace
from src.core.platform_io.types import ConversationContext

from .constants import SCENE_WINDOW_MESSAGES

logger = get_logger(__name__)


class SceneObservationMixin:

    @staticmethod
    def _scene_key(stream_id: int) -> str:
        """返回场景画像在 meta 表里的键名。"""
        return f'scene:{stream_id}'

    def _scene_for_prompt(
        self,
        context: ConversationContext,
    ) -> tuple[str, str] | None:
        """读取当前 stream 的场景画像，供系统提示词渲染。

        只在群聊注入：私聊主动跟进会把刚生成的画像放进专用决策块，不复用群聊
        标题；桌面同样不注入。观察关闭时返回 None，整块省略。

        :param context: 当前会话上下文。
        :return: ``(话题, 气氛)``；没有画像、观察关闭或非群聊时返回 ``None``。
        """
        if (
            context.stream.kind != 'group'
            or self._scene_observer is None
            or self._scene_refresh_messages <= 0
        ):
            return None
        snapshot = SceneSnapshot.from_dict(
            self.memory.read_json(self._scene_key(context.stream.id), None)
        )
        if snapshot is None:
            return None
        return snapshot.topic, snapshot.atmosphere

    def _schedule_scene_observation(self, context: ConversationContext) -> None:
        """按新增消息条数决定要不要在后台重算场景画像。

        观察不进对话的等待路径：作为后台任务完成后写入 meta 表，供后续若干轮读取，
        不影响首字延迟；代价是画像存在时延。

        节流只依据自上次观察以来的新增消息数，不叠加最小时间间隔，
        避免两个互相牵制的节流常量。

        :param context: 当前会话上下文；非群聊或观察关闭时直接返回。
        副作用：可能创建一个后台任务；同一 stream 已有观察在跑时跳过。
        """
        if (
            context.stream.kind != 'group'
            or self._scene_observer is None
            or self._scene_refresh_messages <= 0
        ):
            return
        stream_id = context.stream.id
        if stream_id in self._observing:
            return
        snapshot = SceneSnapshot.from_dict(
            self.memory.read_json(self._scene_key(stream_id), None)
        )
        since = snapshot.observed_message_id if snapshot is not None else 0
        if self.memory.message_count_after(stream_id, since) < self._scene_refresh_messages:
            return
        self._observing.add(stream_id)
        self._track_background_task(
            asyncio.create_task(self._run_scene_observation(context))
        )

    async def _run_scene_observation(self, context: ConversationContext) -> str:
        """在后台读一段群聊历史并写入新的场景画像。

        :param context: 目标群聊上下文。
        :return: 便于后台任务追踪的说明字符串。
        副作用：一次模型调用与一次 meta 表写入；无论成败都释放并发标记。
            观察失败只记日志并保留旧画像：它是附加背景，不影响对话。
        """
        stream_id = context.stream.id
        try:
            messages = self.memory.working_memory(stream_id, SCENE_WINDOW_MESSAGES)
            if not messages:
                return 'scene_observed_empty'
            lines = self._scene_observation_lines(context, messages)
            snapshot = await self._scene_observer.observe(
                lines, messages[-1].message_id,
            )
            self.memory.write_json(self._scene_key(stream_id), snapshot.to_dict())
            trace.emit(
                'scene_observed',
                # 必须显式置空 turnId，否则观察事件在控制台上完全消失：
                # - 现象：观察正常产出并写入画像，终端与 WebUI 日志面板一行都看不到。
                # - 原因：asyncio 任务继承创建时刻的 contextvar 快照，本任务因此带上了
                #   调度它的那个回合的 turnId；控制台出口对带 turnId 的事件一律跳过，
                #   理由是「已由轮末合成面板整体呈现」，而观察跑在面板渲染之后。
                # - 后果：观察不属于任何回合，置空后走独立信息框，两条支路都不会漏。
                turnId=None,
                streamId=stream_id,
                topic=snapshot.topic,
                atmosphere=snapshot.atmosphere,
            )
            return 'scene_observed'
        except Exception as exc:
            logger.warning('scene_observation_failed', streamId=stream_id, error=str(exc))
            return 'scene_observation_failed'
        finally:
            self._observing.discard(stream_id)

    def _scene_observation_lines(
        self,
        context: ConversationContext,
        messages: list[StoredMessage],
    ) -> list[str]:
        """把历史渲染为情景分析 Agent 所需的「说话人：内容」行。

        普通对话提示词的助手历史刻意不加名称，避免模型模仿；情景分析并不生成对话，
        必须显式标清双方，否则私聊里会分不出哪句是 Bot 说的、哪句是对方说的。
        """
        lines: list[str] = []
        for message in messages:
            if message.role == 'assistant':
                speaker = self._bot_display_name
            else:
                if message.sender_person_id is None:
                    raise RuntimeError('情景分析的用户消息缺少发送者人物 ID')
                speaker = self._registry.stream_display_name(
                    message.sender_person_id,
                    context.stream.id,
                )
            lines.append(f'{speaker}: {strip_say_tags(message.content)}')
        return lines
