"""为每个会话维护一段进程内的「会话印象」，用作事实检索的第二检索词。

群聊里当前这条消息经常是短应答，拿它当检索词什么也捞不到；印象是对一段
对话的概括，拿它检索的命中覆盖面完全是另一个量级。印象与当前文本是并集
关系：两次召回的候选合并去重后统一排序，不做先后回退。

状态（上次生成时间、上次消息水位、缓存文本）只在进程内，不落库：重启后
用旧印象比重新生成更糟，而落库会多出一张需要清理的表。

限流只用两个常量：最少新增消息数、缓存 TTL，满足其一才重算。参考实现
在同类位置用了四个限流参数，多余的常量会互相牵制。
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from typing import Callable, Optional

from .history import strip_say_tags, strip_side_effect_tags

from src.core.common.clock import now as current_time
from src.core.llm_models.protocol import LlmProvider
from src.core.llm_models.snapshot import bind_render_params
from src.core.memory.store import MemoryStore, is_assistant_action_message
from src.core.observe import events as trace
from src.core.prompts.registry import get_prompt, prompt_metadata

# 缓存未到期时，重算至少需要的新增消息条数。
IMPRESSION_MIN_NEW_MESSAGES = 30
# 缓存的最长存活时间；到期即使用户没说话也重算，印象不会无限期陈旧。
IMPRESSION_TTL_MS = 10 * 60 * 1000
# 窗口正文短于此长度时不发起模型请求：无可概括内容。
MIN_IMPRESSION_DIALOGUE_CHARS = 40

# 与 person_id、stream_id 对应的显示名解析函数，口径同认知动作的 SpeakerNamer。
SpeakerNamer = Callable[[int, int], str]


@dataclass
class _StreamImpression:
    """单个会话的印象缓存。

    :ivar generated_at: 生成完成的毫秒时间戳。
    :ivar message_id: 生成时该会话已落库的最大消息 ID；新增条数从这里数起。
    :ivar text: 印象正文。
    """

    generated_at: int
    message_id: int
    text: str


def _render_window(
    store: MemoryStore,
    stream_id: int,
    window: int,
    bot_name: str,
    speaker_name: SpeakerNamer,
) -> str:
    """把工作记忆窗口渲染成逐行对话。

    助手动作伪消息整条跳过，真实发言剥协议标签后压成一行，与事实抽取
    的渲染纪律一致：内部协议进入概括模型输入后可能被模仿。

    :param store: 记忆存储实例。
    :param stream_id: 目标会话 ID。
    :param window: 读取的最近消息条数，复用工作记忆窗口大小。
    :param bot_name: Bot 展示名，用于标注 Bot 自己的发言。
    :param speaker_name: 人物显示名解析函数。
    :return: 每行 ``说话人：内容`` 的文本；没有可读内容时返回空字符串。
    副作用：只读 messages 表。
    """

    lines = []
    for message in store.working_memory(stream_id, window):
        if message.role == 'assistant':
            if is_assistant_action_message(message.content):
                continue
            text = strip_say_tags(strip_side_effect_tags(message.content or ''))
            speaker = bot_name
        else:
            text = (message.content or '').strip()
            speaker = speaker_name(message.sender_person_id or -1, stream_id)
        text = ' '.join(part for part in text.splitlines() if part.strip()).strip()
        if text:
            lines.append(f'{speaker}：{text}')
    return '\n'.join(lines)


class ConversationImpressions:
    """按会话生成与缓存会话印象。

    只在达到限流条件时发起一次模型调用；同一会话的并发请求由进程内锁
    串行化，后到者在锁内读到先到者刚写入的缓存，不重复调用。
    """

    def __init__(
        self,
        store: MemoryStore,
        provider: Optional[LlmProvider],
        window_messages: int,
    ) -> None:
        """保存依赖与窗口大小。

        :param store: 记忆存储实例。
        :param provider: memory 任务槽的模型客户端；``None`` 表示没有
            memory 路由，印象整条功能随之关闭。
        :param window_messages: 生成印象时回看的消息条数，复用工作记忆窗口。
        副作用：无。
        """

        self._store = store
        self._provider = provider
        self._window = window_messages
        self._states: dict[int, _StreamImpression] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def current(
        self,
        stream_id: int,
        *,
        bot_name: str,
        speaker_name: SpeakerNamer,
        temperature: float,
        max_tokens: Optional[int],
        now: Optional[int] = None,
    ) -> Optional[str]:
        """返回该会话当前可用的印象，必要时先重算。

        缓存在未达新增条数且未超 TTL 时直接复用；生成失败时丢弃缓存并
        返回 ``None``，调用方据此退回「只用当前文本检索」——失败会发出
        ``memory_impression_failed`` 事件，静默降级会让这条路径的故障
        不可见。

        :param stream_id: 目标会话 ID。
        :param bot_name: Bot 展示名，进系统提示词。
        :param speaker_name: 人物显示名解析函数，用于渲染对话窗口。
        :param temperature: 采样温度。
        :param max_tokens: 输出上限；``None`` 表示由 provider 决定。
        :param now: 可选当前毫秒时间戳；省略时读取统一时钟。
        :return: 印象正文；无可概括内容、模型不可用或生成失败时返回 ``None``。
        副作用：缓存命中时只读；未命中时可能发起一次模型请求并更新进程内缓存。
        """

        now = now if now is not None else current_time()
        lock = self._locks.setdefault(stream_id, asyncio.Lock())
        async with lock:
            state = self._states.get(stream_id)
            if state is not None:
                fresh = (
                    self._store.message_count_after(stream_id, state.message_id)
                    < IMPRESSION_MIN_NEW_MESSAGES
                    and now - state.generated_at < IMPRESSION_TTL_MS
                )
                if fresh:
                    return state.text
            return await self._regenerate(
                stream_id,
                bot_name=bot_name,
                speaker_name=speaker_name,
                temperature=temperature,
                max_tokens=max_tokens,
                now=now,
            )

    async def _regenerate(
        self,
        stream_id: int,
        *,
        bot_name: str,
        speaker_name: SpeakerNamer,
        temperature: float,
        max_tokens: Optional[int],
        now: int,
    ) -> Optional[str]:
        """重新生成印象并写入缓存；失败时清掉旧缓存。

        :return: 新印象正文；前置条件不满足或模型往返失败时返回 ``None``。
        副作用：可能发起一次模型请求；成功或失败都会更新进程内缓存状态。
        """

        if self._provider is None:
            return None
        dialogue = _render_window(
            self._store, stream_id, self._window, bot_name, speaker_name,
        )
        if len(dialogue) < MIN_IMPRESSION_DIALOGUE_CHARS:
            return None
        render_params = {'memory.impression': {'bot_name': bot_name}}
        request_messages = [
            {
                'role': 'system',
                'content': get_prompt('memory.impression').render(bot_name=bot_name),
            },
            {'role': 'user', 'content': dialogue},
        ]
        raw = ''
        try:
            trace.emit(
                'llm_request',
                messages=request_messages,
                temperature=temperature,
                maxTokens=max_tokens,
                renderParams=render_params,
                **prompt_metadata('memory.impression', ('memory.impression',)),
            )
            bind_render_params(render_params)
            async for chunk in self._provider.stream(
                messages=request_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            ):
                if chunk.get('text'):
                    raw += chunk['text']
        except Exception as exc:
            # 失败必须可见：退回只用当前文本检索是既定降级，但静默降级会让
            # 这条路径的故障与「这段对话确实没什么可记」无法区分。
            trace.emit(
                'memory_impression_failed',
                streamId=stream_id,
                error=f'{type(exc).__name__}：{exc}',
            )
            self._states.pop(stream_id, None)
            return None
        text = raw.strip()
        if not text:
            trace.emit('memory_impression_failed', streamId=stream_id, error='空输出')
            self._states.pop(stream_id, None)
            return None
        self._states[stream_id] = _StreamImpression(
            generated_at=now,
            message_id=self._store.latest_message_id(stream_id),
            text=text,
        )
        trace.emit('memory_impression_refreshed', streamId=stream_id, chars=len(text))
        return text
