"""私聊跟进与输入状态催促。

本 mixin 维护一段私聊静默的锚点：记录正常回复时刻、按配置安排定时追问、
在对方开始输入时决定要不要催一句，并在新消息到达时撤掉待发的跟进。

群聊不走这条路径——群里无人应答是常态，追问会变成打扰。

由 ``ChatService`` 继承，依赖它的会话状态与配置属性。
"""

import asyncio

from src.core.agent.observer import SceneSnapshot
from src.core.memory.store import StoredMessage
from src.core.platform_io.types import ConversationContext
from src.core.runtime.clock import now as current_time

from .state import _DirectFollowUpState


class DirectFollowUpMixin:

    def _typing_silence_anchor(self, stream_id: int, last_reply_at: int) -> int:
        """返回输入状态检测使用的静默起点。

        一分钟定时追问属于同一段静默，不能把三分钟输入检测推迟到第四分钟；只有
        当前最后一条助手消息仍是本状态里的正常回复或定时追问时，才复用原始锚点。
        其他主动消息或输入状态追问会成为新的最后回复，并自然恢复原有计时口径。
        """
        state = self._direct_follow_ups.get(stream_id)
        if state is None:
            return last_reply_at
        if last_reply_at in (
            state.normal_reply_message_at,
            state.follow_up_message_at,
        ):
            return state.silence_started_at
        return last_reply_at

    def _cancel_direct_follow_up(self, stream_id: int) -> None:
        """结束指定私聊的当前静默状态，并取消尚未完成的定时追问。"""
        state = self._direct_follow_ups.pop(stream_id, None)
        if state is None or state.task is None or state.task.done():
            return
        if state.task is not asyncio.current_task():
            state.task.cancel()

    def _arm_direct_follow_up(self, context: ConversationContext) -> None:
        """在一次成功的私聊正常回复后安排唯一一轮情景决策机会。"""
        stream_id = context.stream.id
        self._cancel_direct_follow_up(stream_id)
        follow_up = self._cfg.typing.follow_up
        if (
            context.stream.kind != 'direct'
            or not follow_up.enabled
            or self._agent_scope(context, 'deliberate') != 'live'
            or self._poll_task is None
            or self._stop.is_set()
            or self._buffers.get(stream_id)
        ):
            return
        normal_reply_message_at = self.memory.last_assistant_reply_at(stream_id)
        if normal_reply_message_at is None:
            return
        target = self._follow_up_target(context)
        if target is None:
            return
        state = _DirectFollowUpState(
            context=context,
            silence_started_at=current_time(),
            target_user_message_id=target.message_id,
            normal_reply_message_at=normal_reply_message_at,
        )
        self._direct_follow_ups[stream_id] = state
        state.task = asyncio.create_task(
            self._run_direct_follow_up(state),
            name=f'direct-follow-up-{stream_id}',
        )

    def _follow_up_target(
        self,
        context: ConversationContext,
        state: _DirectFollowUpState | None = None,
    ) -> StoredMessage | None:
        """读取本次主动决策可回复的最近一条真实用户消息。

        定时任务保存消息主键，后续输入状态必须继续指向同一段静默的原始消息；没有
        状态时则取当前工作记忆里的最后一条用户消息。找不到目标就放弃，而不是构造
        不可审计的虚拟消息。
        """
        history = self.memory.working_memory(
            context.stream.id,
            self._working_memory_messages,
        )
        target_id = state.target_user_message_id if state is not None else None
        for message in reversed(history):
            if message.role == 'user' and (
                target_id is None or message.message_id == target_id
            ):
                return message
        return None

    @staticmethod
    def _with_direct_scene_analysis(
        messages: list[dict],
        scene: SceneSnapshot,
        *,
        itemized: bool = False,
    ) -> list[dict]:
        """把独立情景分析结果放进私聊动作决策上下文。

        :param messages: 首项必须是 system 的已渲染模型消息。
        :param scene: SceneObserver 刚产出的合法场景画像。
        :param itemized: 是否把画像追加为独立 user item；传统角色模式仍并入 system。
        :return: 已加入画像的新消息列表；调用方原列表保持不变。
        :raises ValueError: 消息列表为空或首项不是 system 时抛出。
        """
        if not messages or messages[0].get('role') != 'system':
            raise ValueError('私聊情景分析只能注入以 system 开头的模型消息')
        scene_context = '\n'.join((
            '# 私聊情景分析结果',
            f'当前话题：{scene.topic}',
            f'当前气氛：{scene.atmosphere}',
            '这是独立情景分析 Agent 对当前互动的描述，只提供决策背景。'
            '是否回复仍由 Conversation Agent 根据动作空间自行判断。',
        ))
        if itemized:
            return [
                *messages,
                {'role': 'user', 'content': scene_context},
            ]
        system = {
            **messages[0],
            'content': '\n\n'.join((
                messages[0]['content'],
                scene_context,
            )),
        }
        return [system, *messages[1:]]

    @staticmethod
    def _scheduled_follow_up_situation(silence_ms: int) -> str:
        """把达到配置阈值的未回复状态渲染为第一次主动机会。"""
        minutes = max(1, silence_ms // 60_000)
        return '\n'.join((
            f'你上一句发出去已经 {minutes} 分钟了，对方一直没有回复。',
            '这是这段静默里的第一次主动跟进机会，不代表一定要追问。',
            '若决定开口，请顺着刚才的话自然说一句，不要报时，也不要提系统、计时器。',
        ))

    @staticmethod
    def _typing_situation(silence_ms: int, nudges: int) -> str:
        """把等待时长与已催次数渲染成 Bot 的主观感受。

        情境写成 Bot 看到的事实而不是系统报告：模型据此自行选择语气，代码不规定
        这次该催还是该缓和。

        :param silence_ms: Bot 上次发言至今的静默毫秒数。
        :param nudges: 本次静默期内已经催过的次数。
        :return: 交给主动消息模型的情境文本。
        """
        minutes = silence_ms // 60_000
        lines = [
            f'你上一句发出去已经 {minutes} 分钟了，他一直没回。',
            '现在你看到他那边开始打字了，话还没发出来。',
        ]
        if nudges:
            lines.append(f'这段时间里你已经催过 {nudges} 次。')
        lines.append('这是一次可选的跟进机会，不是必须开口。')
        lines.append(
            '若决定开口，就顺着这个情形自然地说一句，别复述你等了多久，'
            '也别提「输入状态」这种词。'
        )
        return '\n'.join(lines)
