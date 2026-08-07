"""
对话编排服务。直接移植自 src/main/chat.ts。

持有所有后端资源：按角色拆分的 LLM provider、MemoryStore、Persona、DayPlanService。
通过 WebSocket push 推事件给 Electron 主进程。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import asyncio
import inspect
import random

from .trace import bind_origin, trace
from .trace_console import mark_turn_start, render_turn, render_turn_error
from .vector import VectorService

from src.agent.character import pick_tone
from src.agent.expression import render_expression_habits, select_expression_habits
from src.agent.history import close_dangling_say, fit_char_budget, normalize_history
from src.agent.parser import (
    MemoryEvent, MoodEvent, ParseEvent, PromiseEvent, ResponseParser, SayEndEvent, SayEvent, TextEvent,
)
from src.agent.prompt import build_proactive_prompt, build_system_prompt, describe_resumption
from src.agent.summarize import summarize
from src.awareness.sleep import SleepState
from src.common.clock import now as current_time
from src.common.logger import get_logger
from src.memory.store import EpisodeInput, FactInput, MemoryStore
from src.persona.state import MoodDelta, Persona, describe_acquaintance, describe_persona
from src.platform_io.broker import PlatformBroker
from src.platform_io.registry import StreamRegistry
from src.platform_io.types import ConversationContext, InboundMessage, OutboundMessage
from src.schedule.plan import DayPlanService, ScheduleSleepState

logger = get_logger(__name__)

# 工作记忆窗口：进 context 的最近消息条数
WINDOW = 40
# 待压缩消息超过这个数就触发一次 L2 摘要
SUMMARIZE_AT = 48
# 每次摘要吃掉多少条最老的消息
SUMMARIZE_BATCH = 16
# 真正的天花板是 min(WINDOW, pending_count)——working_memory() 只取
# episode_id IS NULL 的消息。所以 SUMMARIZE_AT 必须跟着 WINDOW 一起调，
# 否则摘要一跑 pending 就掉到 SUMMARIZE_AT - SUMMARIZE_BATCH，
# 单调 WINDOW 完全无效。当前下限 48-16=32 条 ≈ 16 轮。

# 两次对话间隔超过这个时长，视为新一段对话：重新抽语气、重新播种表达样本。
SESSION_GAP_MS = 30 * 60_000

_HINTS: dict[str, str] = {
    'auth': 'API Key 无效，检查 providers.toml',
    'model': '模型 ID 不对，检查 models.toml',
    'quota': '限流或余额不足，稍等一下',
    'network': '连不上模型接口，检查网络或代理',
    'blocked': '这句被内容审核拦了，换个说法',
}


@dataclass
class _InflightTurn:
    """可被指定 stream 打断的一次流式对话。"""

    task: asyncio.Task[None]
    cancel_event: asyncio.Event


@dataclass
class _SessionState:
    """一个 stream 内稳定的语气、表达样本和单次重逢上下文。"""

    started_at: int | None = None
    tone: str | None = None
    seed: int = 0
    resumption_gap_ms: int | None = None


class ChatService:
    """
    对话编排。

    push_event: 把事件推送到 WebSocket 的回调，由 api/ws.py 注入。
    speak_audio: 把文本送去合成的回调，由 tts service 注入（可 None）。
    """

    def __init__(
        self,
        db: Any,
        chat_provider: Any | None,
        proactive_provider: Any | None,
        summary_provider: Any | None,
        push_event: Callable[[str, Any, int], Any],
        speak_audio: Callable[[str, int], Any] | None = None,
        vector: VectorService | None = None,
        cfg: Any | None = None,
        broker: PlatformBroker | None = None,
    ) -> None:
        self._db = db
        self._chat_provider = chat_provider
        self._proactive_provider = proactive_provider
        self._summary_provider = summary_provider
        self._push_event = push_event
        self._speak_audio = speak_audio
        self._broker = broker
        # 打断时用来叫停已经在播的音频；由 __main__ 注入 TtsService.cancel。
        self._cancel_audio: Callable[[int], Any] | None = None
        # 流式解析时按 stream 攒当前这句 <say> 的正文，收完整句才送去合成。
        self._speech_buffer: dict[int, list[str]] = {}
        self._vector = vector or VectorService(None, None)
        self._cfg = cfg
        if cfg is None:
            self._working_memory_messages = WINDOW
            self._summarize_trigger_messages = SUMMARIZE_AT
            self._summarize_batch_messages = SUMMARIZE_BATCH
            self._session_gap_ms = SESSION_GAP_MS
            self._fact_recall_limit = 6
            self._recalled_episode_limit = 2
            self._recent_episode_limit = 2
            self._episode_context_limit = 3
            self._chat_temperature = 0.85
            self._chat_max_tokens = None
            self._proactive_temperature = 0.9
            self._proactive_max_tokens = 200
            self._summary_temperature = 0.3
            self._summary_max_tokens = None
        else:
            conversation = cfg.conversation
            generation = cfg.generation
            self._working_memory_messages = conversation.working_memory_messages
            self._summarize_trigger_messages = conversation.summarize_trigger_messages
            self._summarize_batch_messages = conversation.summarize_batch_messages
            self._session_gap_ms = conversation.session_gap_minutes * 60_000
            self._fact_recall_limit = conversation.fact_recall_limit
            self._recalled_episode_limit = conversation.recalled_episode_limit
            self._recent_episode_limit = conversation.recent_episode_limit
            self._episode_context_limit = conversation.episode_context_limit
            self._chat_temperature = generation.chat.temperature
            self._chat_max_tokens = generation.chat.token_limit
            self._proactive_temperature = generation.proactive.temperature
            self._proactive_max_tokens = generation.proactive.token_limit
            self._summary_temperature = generation.summary.temperature
            self._summary_max_tokens = generation.summary.token_limit
        self.memory = MemoryStore(db)
        self._registry = StreamRegistry(db)
        self._desktop_context = self._registry.desktop_context()
        self.persona = Persona(db)
        self.persona.snapshot_daily(self._desktop_context.person.id)
        self._turn_id = 0
        self._inflight: dict[int, _InflightTurn] = {}
        self._sessions: dict[int, _SessionState] = {}
        self._summarizing: set[int] = set()
        self._active_turns: dict[int, int] = {}
        self._activity: Callable[[], str] | None = None
        self._sleep_state: Callable[[], SleepState] | None = None
        self._promise_handler: Callable[[int, str], None] | None = None
        self._schedule: DayPlanService | None = None

    @property
    def ready(self) -> bool:
        return self._chat_provider is not None

    @property
    def desktop_context(self) -> ConversationContext:
        """当前 desktop 链路的归属上下文；M1.4.5 再改为每条入站消息显式携带。"""
        return self._desktop_context

    def set_schedule(self, svc: DayPlanService) -> None:
        self._schedule = svc

    def set_activity_provider(self, fn: Callable[[], str]) -> None:
        self._activity = fn

    def set_sleep_state_provider(self, fn: Callable[[], SleepState]) -> None:
        self._sleep_state = fn

    def set_promise_handler(self, fn: Callable[[int, str], None]) -> None:
        """接收解析出的约定，交由 AwarenessService 统一调度与持久化。"""
        self._promise_handler = fn

    def current_sleep(self) -> ScheduleSleepState:
        s = self._sleep_state() if self._sleep_state else None
        if s is None:
            return ScheduleSleepState(asleep=False, drowsy=False)
        return ScheduleSleepState(asleep=s.asleep, drowsy=s.drowsy, just_woke=s.just_woke)

    async def ensure_schedule(self, now: int | None = None) -> None:
        if self._schedule:
            await self._schedule.ensure(now or current_time())

    def settle_elapsed(
        self,
        context: ConversationContext,
        now: int | None = None,
        earlier_asleep: bool = False,
    ) -> None:
        now = now or current_time()
        person_id = context.person.id
        before = self.persona.get(person_id)
        if self._schedule:
            asleep_hours = self._schedule.sleep_hours_between(before.updated_at, now, earlier_asleep)
        else:
            asleep_hours = 0.0
        if context.relationship_signals_enabled:
            self.persona.apply_elapsed(person_id, now, asleep_hours)
            self.persona.snapshot_daily(person_id, now)

    async def send(self, inbound: InboundMessage) -> int:
        """处理一条携带完整归属上下文的入站消息。"""
        context = inbound.context
        stream_id = context.stream.id
        trimmed = inbound.text.strip()
        if not trimmed:
            return self._turn_id

        self.interrupt(stream_id)
        turn = self._next_turn()
        self._active_turns[stream_id] = turn
        mark_turn_start(turn)
        # 绑定来源，这一轮后续的每条 trace 都会带上，不必逐个 kind 拼
        bind_origin(
            stream_id=stream_id,
            platform=context.stream.platform,
            person_id=context.person.id,
            person_kind=context.person.kind,
        )
        trace.emit('user_input', turnId=turn, text=trimmed)

        if not self._chat_provider:
            await self._emit(stream_id, 'chat.error', {
                'turnId': turn,
                'kind': 'error',
                'message': '对话未初始化',
                'hint': '检查 providers.toml 和 models.toml',
            })
            return turn

        cancel_event = asyncio.Event()

        async def _run() -> None:
            now = current_time()
            asleep = self._sleep_state().asleep if self._sleep_state else False
            self.settle_elapsed(context, now, asleep)
            self.memory.sweep(now)
            if self._schedule:
                self._schedule.ensure_background(now)

            # ★ 必须在 append 之前判定：append 之后 last_message_at() 就是 now，
            #   间隔恒为 0，会话永远不会翻页。
            self._refresh_session(context, now)
            user_msg_id = self.memory.append_message(
                stream_id,
                context.person.id,
                'user',
                trimmed,
                now,
            )
            if cancel_event.is_set():
                return

            parser = ResponseParser()
            assistant_raw = ''
            side_effects: list[dict] = []
            outbound_segments: list[str] = []
            outbound_segment: list[str] | None = None
            interrupted = False
            reply_persisted = False
            from src.llm_models.openai import LlmError
            try:
                messages = await self._build_messages_with_vector(context, trimmed, now)
                trace.emit(
                    'llm_request',
                    turnId=turn,
                    messages=messages,
                    temperature=self._chat_temperature,
                    maxTokens=self._chat_max_tokens,
                )
                async for chunk in self._chat_provider.stream(
                    messages=messages,
                    temperature=self._chat_temperature,
                    max_tokens=self._chat_max_tokens,
                ):
                    if cancel_event.is_set():
                        interrupted = True
                        break
                    trace.emit('llm_chunk', turnId=turn, text=chunk.get('text'), reasoning=chunk.get('reasoning'))
                    if not chunk.get('text'):
                        continue
                    assistant_raw += chunk['text']
                    for event in parser.push(chunk['text']):
                        if cancel_event.is_set():
                            interrupted = True
                            break
                        self._handle_side_effects(context, event, now, turn, side_effects, trimmed)
                        if context.stream.platform == 'desktop':
                            self._track_speech(context, event, turn)
                            await self._emit_parse_event(context, turn, event)
                        else:
                            outbound_segment = _collect_outbound_segment(
                                event,
                                outbound_segments,
                                outbound_segment,
                            )
                    if interrupted:
                        break

                if not interrupted:
                    for event in parser.flush():
                        if cancel_event.is_set():
                            interrupted = True
                            break
                        self._handle_side_effects(context, event, now, turn, side_effects, trimmed)
                        if context.stream.platform == 'desktop':
                            self._track_speech(context, event, turn)
                            await self._emit_parse_event(context, turn, event)
                        else:
                            outbound_segment = _collect_outbound_segment(
                                event,
                                outbound_segments,
                                outbound_segment,
                            )

                # ★ 中断也要落库。这段话已经显示（甚至念）给用户了，历史里
                #   不能当它没发生过——否则下一轮就是连着两条 user 消息，
                #   模型看不到自己上一句说了什么。副作用（<memory>/<mood>）
                #   在流式过程中已经写库了，话本身更不该丢。
                self._persist_reply(context, assistant_raw)
                reply_persisted = True
                if interrupted:
                    return

                trace.emit('llm_final', turnId=turn, text=assistant_raw)
                render_turn(turn, trimmed, messages, assistant_raw, side_effects)
                try:
                    self.persona.apply_turn(context.person.id, current_time())
                except Exception as exc:
                    # 人格推进失败不该把历史一起拖下水——下面的 except 会删用户消息。
                    logger.warning('persona_apply_turn_failed', turnId=turn, error=str(exc))
                if context.stream.platform == 'desktop':
                    await self._emit(stream_id, 'chat.done', {'turnId': turn, 'kind': 'done'})
                else:
                    await self._dispatch_outbound(
                        context,
                        turn,
                        outbound_segments,
                    )
                asyncio.create_task(self._maybe_summarize(context.stream.id))

            except LlmError as exc:
                if exc.kind == 'aborted':
                    if not reply_persisted:
                        self._persist_reply(context, assistant_raw)
                    return
                if not reply_persisted:
                    self._rollback_or_keep(context, user_msg_id, assistant_raw)
                hint = _HINTS.get(exc.kind, '')
                trace.emit('llm_error', turnId=turn, errorKind=exc.kind, message=str(exc))
                render_turn_error(turn, trimmed, exc.kind, str(exc))
                if context.stream.platform == 'desktop':
                    await self._emit(stream_id, 'chat.error', {
                        'turnId': turn,
                        'kind': 'error',
                        'message': str(exc),
                        'hint': hint,
                    })
            except Exception as exc:
                if not reply_persisted:
                    self._rollback_or_keep(context, user_msg_id, assistant_raw)
                trace.emit('llm_error', turnId=turn, errorKind='unknown', message=str(exc))
                render_turn_error(turn, trimmed, 'unknown', str(exc))
                if context.stream.platform == 'desktop':
                    await self._emit(
                        stream_id,
                        'chat.error',
                        {'turnId': turn, 'kind': 'error', 'message': str(exc)},
                    )

        task = asyncio.create_task(_run())
        inflight = _InflightTurn(task=task, cancel_event=cancel_event)
        self._inflight[stream_id] = inflight

        def _remove_completed(done_task: asyncio.Task[None]) -> None:
            current = self._inflight.get(stream_id)
            if current is inflight and current.task is done_task:
                self._inflight.pop(stream_id, None)

        task.add_done_callback(_remove_completed)
        return turn

    def _session(self, stream_id: int) -> _SessionState:
        state = self._sessions.get(stream_id)
        if state is None:
            state = _SessionState()
            self._sessions[stream_id] = state
        return state

    def _refresh_session(self, context: ConversationContext, now: int) -> int | None:
        """跨过静默间隔就开一段新会话，重抽语气和表达样本的随机种子。

        返回本次静默了多久（毫秒）；仍在同一会话内则返回 None。
        首次启动（库里一条消息都没有）也返回 None —— 没有「上一次」可言。
        """
        stream_id = context.stream.id
        state = self._session(stream_id)
        last = self.memory.last_message_at(stream_id)
        gap_ms = now - last if last is not None else None
        if (state.started_at is None
                or last is None
                or (gap_ms is not None and gap_ms > self._session_gap_ms)):
            state.started_at = now
            if self._cfg is None:
                state.tone = pick_tone()
            else:
                personality = self._cfg.personality
                state.tone = pick_tone(
                    probability=personality.tone_probability,
                    variants=personality.tone_variants,
                )
            state.seed = random.randrange(1 << 30)
        if (not context.relationship_signals_enabled
                or gap_ms is None
                or gap_ms <= self._session_gap_ms):
            state.resumption_gap_ms = None
        else:
            state.resumption_gap_ms = gap_ms
        return state.resumption_gap_ms

    def _take_resumption(self, stream_id: int) -> str | None:
        """取走本轮重逢事实，确保它只进入一次系统提示词。"""
        state = self._session(stream_id)
        gap_ms = state.resumption_gap_ms
        state.resumption_gap_ms = None
        if gap_ms is None:
            return None
        return describe_resumption(gap_ms)

    def _session_rng(self, stream_id: int) -> random.Random:
        """同一会话内给出同一批表达样本；情境识别仍然逐轮进行。"""
        return random.Random(self._session(stream_id).seed)

    def _persist_reply(self, context: ConversationContext, assistant_raw: str) -> None:
        """把已经吐出去的回复落进历史，补齐流式中断留下的悬空 <say>。"""
        text = close_dangling_say(assistant_raw)
        if text:
            self.memory.append_message(
                context.stream.id,
                None,
                'assistant',
                text,
                current_time(),
            )

    def _rollback_or_keep(
        self,
        context: ConversationContext,
        user_msg_id: int,
        assistant_raw: str,
    ) -> None:
        """出错时决定这一轮留不留。

        一个字都没吐出来 → 整轮回滚，用户那句话也删掉（保持原有意图：
        这轮相当于没发生）。已经有内容显示给用户了 → 两条都留下，
        删掉用户消息反而会让历史里出现「凭空的回复」。
        """
        if close_dangling_say(assistant_raw):
            self._persist_reply(context, assistant_raw)
        else:
            self.memory.delete_message(context.stream.id, user_msg_id)

    def interrupt(self, stream_id: int) -> None:
        """只取消指定 stream 的对话、语音和半截台词。"""
        inflight = self._inflight.pop(stream_id, None)
        if inflight is not None and not inflight.task.done():
            inflight.cancel_event.set()
        # 半截台词的缓冲不能留到下一轮，否则会把上一句的尾巴念进新回复里。
        self._speech_buffer.pop(stream_id, None)
        turn = self._active_turns.pop(stream_id, None)
        if self._cancel_audio and turn is not None:
            try:
                result = self._cancel_audio(turn)
                if inspect.isawaitable(result):
                    asyncio.create_task(result)
            except Exception as exc:
                logger.warning('cancel_audio_failed', error=str(exc))

    def speak(self, context: ConversationContext, lines: list[dict]) -> int:
        if not lines:
            return self._turn_id
        stream_id = context.stream.id
        self.interrupt(stream_id)
        turn = self._next_turn()
        self._active_turns[stream_id] = turn
        texts: list[str] = []
        for line in lines:
            asyncio.create_task(
                self._emit_parse_event(context, turn, SayEvent(emotion=line.get('emotion')))
            )
            asyncio.create_task(self._emit_parse_event(context, turn, TextEvent(value=line['text'])))
            asyncio.create_task(self._emit_parse_event(context, turn, SayEndEvent()))
            self._dispatch_speech(context, line['text'], turn)
            texts.append(f'<say>{line["text"]}</say>')
        asyncio.create_task(self._emit(context.stream.id, 'chat.done', {'turnId': turn, 'kind': 'done'}))
        self.memory.append_message(
            stream_id,
            None,
            'assistant',
            ''.join(texts),
        )
        return turn

    async def compose_proactive(
        self,
        context: ConversationContext,
        situation: str,
    ) -> list[dict] | None:
        if not self._proactive_provider:
            return None
        now = current_time()
        self._refresh_session(context, now)
        if self._schedule:
            await self._schedule.ensure(now)
        persona_desc = describe_persona(self.persona.get(context.person.id))
        acquaintance = describe_acquaintance(
            self.memory.first_seen_at(context.person.id),
            now,
        )
        schedule_desc = (self._schedule.describe(now, self.current_sleep())
                         if self._schedule else '')
        base_prompt = build_system_prompt(
            now=datetime.fromtimestamp(now / 1000),
            persona=persona_desc,
            acquaintance=acquaintance,
            facts=[
                fact.content
                for fact in self.memory.top_facts(context.person.id, 5, now)
            ],
            episodes=[episode.summary for episode in self.memory.recent_episodes(
                context.stream.id, 2
            )],
            schedule=schedule_desc,
            expression_habits=render_expression_habits(
                select_expression_habits(
                    situation,
                    proactive=True,
                    limit=3,
                    rng=self._session_rng(context.stream.id),
                )
            ),
            tone=self._session(context.stream.id).tone,
            resumption=self._take_resumption(context.stream.id),
            **self._prompt_config_kwargs(),
        )
        system = build_proactive_prompt(base_prompt, situation)
        raw = ''
        try:
            async for chunk in self._proactive_provider.stream(
                messages=[{'role': 'system', 'content': system}],
                temperature=self._proactive_temperature,
                max_tokens=self._proactive_max_tokens,
            ):
                if chunk.get('text'):
                    raw += chunk['text']
        except Exception:
            return None
        return _extract_lines(raw)

    def diary_payload(self, now: int | None = None) -> dict:
        now = now or current_time()
        memories = [
            {'content': fact.content, 'frozen': fact.frozen}
            for fact in self.memory.all_facts(self._desktop_context.person.id, now)
        ]
        today = self._schedule.get(now) if self._schedule else None
        return {
            'entries': self.memory.all_episodes(),
            'today': _plan_to_dict(today) if today else None,
            'memories': memories,
            'now': now,
        }

    def observability_snapshot(self, now: int | None = None) -> dict:
        now = now or current_time()
        s = self.persona.get(self._desktop_context.person.id)
        fc = self.memory.fact_count(self._desktop_context.person.id)
        return {
            'now': now,
            'persona': {'state': s.__dict__, 'description': describe_persona(s)},
            'schedule': _plan_to_dict(self._schedule.get(now)) if self._schedule else None,
            'memory': {
                'semantic': [
                    fact.__dict__
                    for fact in self.memory.all_facts(self._desktop_context.person.id, now)
                ],
                'episodes': len(self.memory.all_episodes()),
                'workingMessages': self.memory.pending_count(self._desktop_context.stream.id),
            },
        }

    def _next_turn(self) -> int:
        self._turn_id += 1
        return self._turn_id

    def _relationship_kwargs(self) -> dict:
        """从 cfg.bot 取用户昵称/关系称呼，喂给 build_system_prompt。cfg 可以是 None
        （比如测试直接构造 ChatService 不传 cfg），这时两项都不注入，行为和之前一样。"""
        if self._cfg is None:
            return {}
        bot_cfg = self._cfg.bot
        return {
            'user_nickname': bot_cfg.user_nickname,
            'relationship': bot_cfg.relationship,
        }

    def _prompt_config_kwargs(self) -> dict:
        """把 bot.toml 中的身份、人格和用户关系一次性注入系统提示词。"""
        if self._cfg is None:
            return {}
        bot = self._cfg.bot
        personality = self._cfg.personality
        return {
            'name': bot.name,
            'user_nickname': bot.user_nickname,
            'relationship': bot.relationship,
            'identity': personality.identity,
            'behavior': personality.behavior,
            'reply_style': personality.reply_style,
            'attention': personality.attention,
            'boundaries': personality.boundaries,
        }

    async def _build_messages_with_vector(
        self,
        context: ConversationContext,
        query: str,
        now: int,
    ) -> list[dict]:
        """向量召回版本的消息构建。_build_messages 的异步替代。"""
        query_embedding = await self._vector.embed_query(query)
        facts = self.memory.recall_facts(
            context.person.id,
            query,
            self._fact_recall_limit,
            now,
            query_embedding=query_embedding,
        )
        recalled = self.memory.recall_episodes(
            context.stream.id,
            query,
            self._recalled_episode_limit,
        )
        recent = self.memory.recent_episodes(
            context.stream.id,
            self._recent_episode_limit,
        )
        seen_ids: set[int] = set()
        episodes = []
        for e in [*recalled, *recent]:
            if e.id not in seen_ids:
                seen_ids.add(e.id)
                episodes.append(e)
        episodes = episodes[:self._episode_context_limit]
        persona_desc = describe_persona(self.persona.get(context.person.id))
        acquaintance = describe_acquaintance(
            self.memory.first_seen_at(context.person.id),
            now,
        )
        schedule_desc = (self._schedule.describe(now, self.current_sleep()) if self._schedule else None)
        resumption = self._take_resumption(context.stream.id)
        system = build_system_prompt(
            now=datetime.fromtimestamp(now / 1000),
            persona=persona_desc,
            acquaintance=acquaintance,
            facts=[f.content for f in facts],
            episodes=[e.summary for e in episodes],
            activity=(self._activity() if self._activity else None),
            schedule=schedule_desc,
            expression_habits=render_expression_habits(
                select_expression_habits(query, rng=self._session_rng(context.stream.id))
            ),
            tone=self._session(context.stream.id).tone,
            resumption=resumption,
            **self._prompt_config_kwargs(),
        )
        # ★ 读时修复：不假设历史是干净的。库里已经存在的坏历史（每一次打断
        #   都损坏过一轮）只能在这里救回来，写入端的修复管不到已经写坏的部分。
        #   幂等，对干净历史没有副作用。
        wm = self.memory.working_memory(
            context.stream.id,
            self._working_memory_messages,
        )
        history = normalize_history(self._history_for_context(context, wm))
        return [{'role': 'system', 'content': system}, *fit_char_budget(history)]

    def _history_for_context(self, context: ConversationContext, messages: list[Any]) -> list[dict]:
        """仅在组装群聊历史时补说话人显示名，不污染原始消息内容。"""
        history: list[dict] = []
        for message in messages:
            content = message.content
            if context.stream.kind == 'group' and message.role == 'user':
                if message.sender_person_id is None:
                    raise RuntimeError('群聊 user 历史缺少 sender_person_id')
                name = self._registry.display_name(message.sender_person_id, context.stream.platform)
                content = f'{name}: {content}'
            history.append({'role': message.role, 'content': content})
        return history

    def _handle_side_effects(
        self, context: ConversationContext, event: ParseEvent, now: int, turn: int,
        sink: list[dict] | None = None,
        source_text: str | None = None,
    ) -> None:
        if isinstance(event, MemoryEvent) and event.content:
            memory_kind = event.memory_type or '未分类'
            self.memory.add_fact(
                context.person.id,
                FactInput(content=event.content, kind=memory_kind),
                now,
            )
            trace.emit('memory_fact', turnId=turn, content=event.content, memoryKind=memory_kind)
            if sink is not None:
                sink.append({'kind': 'memory_fact', 'content': event.content, 'memoryKind': memory_kind})
        elif isinstance(event, MoodEvent):
            self.persona.apply_mood(
                context.person.id,
                MoodDelta(favor=event.favor, energy=event.energy),
                now,
            )
            trace.emit('mood_delta', turnId=turn, favor=event.favor, energy=event.energy)
            if sink is not None:
                sink.append({'kind': 'mood_delta', 'favor': event.favor, 'energy': event.energy})
        elif isinstance(event, PromiseEvent):
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
        """把一句台词送去合成。

        ★ 这里必须容忍同步和协程两种回调：注入进来的 TtsService.speak 是同步的
          （它自己内部起 task），而此前调用点写的是
          `asyncio.create_task(self._speak_audio(...))` —— 对同步函数来说等于
          `create_task(None)`，直接抛 TypeError。主动搭话那条语音链路一直是
          这么坏掉的。
        """
        if context.stream.kind != 'desktop' or not self._speak_audio:
            return
        line = text.strip()
        if not line:
            return
        try:
            result = self._speak_audio(line, turn)
            if inspect.isawaitable(result):
                asyncio.create_task(result)
        except Exception as exc:
            logger.warning('speak_audio_failed', turnId=turn, error=str(exc))

    def _track_speech(self, context: ConversationContext, event: ParseEvent, turn: int) -> None:
        """在流式解析过程中攒出完整台词，每收完一个 <say> 就送去合成。

        ★ 此前这里是个空 stub，导致正常对话**完全不发声**——_speak_audio
          全仓库只在 speak()（主动搭话）里被调用过。按 <say> 分句送，而不是
          等整段回复收完，是为了让她开口的延迟和字幕对得上。
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

    async def _emit(self, stream_id: int, channel: str, payload: Any) -> None:
        try:
            await self._push_event(channel, payload, stream_id)
        except Exception as exc:
            logger.warning('emit_failed', streamId=stream_id, channel=channel, error=str(exc))

    async def _emit_parse_event(
        self,
        context: ConversationContext,
        turn: int,
        event: ParseEvent,
    ) -> None:
        if context.stream.platform != 'desktop':
            return
        if isinstance(event, SayEvent):
            ev = {'turnId': turn, 'kind': 'parse',
                  'event': {'type': 'say', **({'emotion': event.emotion} if event.emotion else {}),
                             **({'gesture': event.gesture} if event.gesture else {})}}
        elif isinstance(event, TextEvent):
            ev = {'turnId': turn, 'kind': 'parse', 'event': {'type': 'text', 'value': event.value}}
        elif isinstance(event, SayEndEvent):
            ev = {'turnId': turn, 'kind': 'parse', 'event': {'type': 'sayEnd'}}
        elif isinstance(event, MemoryEvent):
            ev = {'turnId': turn, 'kind': 'parse',
                  'event': {'type': 'memory', 'content': event.content,
                             **({'memoryType': event.memory_type} if event.memory_type else {})}}
        elif isinstance(event, MoodEvent):
            ev = {'turnId': turn, 'kind': 'parse',
                  'event': {'type': 'mood',
                             **({'favor': event.favor} if event.favor is not None else {}),
                             **({'energy': event.energy} if event.energy is not None else {})}}
        else:
            return
        await self._emit(context.stream.id, 'chat.event', ev)

    async def _dispatch_outbound(
        self,
        context: ConversationContext,
        turn: int,
        segments: list[str],
    ) -> None:
        """把非桌面整轮回复交给 broker，桌面永远不走这条路径。"""
        if context.stream.platform == 'desktop':
            raise RuntimeError('desktop stream 不能经由非桌面 broker 投递')
        if self._broker is None:
            raise RuntimeError('非桌面 stream 未配置 PlatformBroker')
        if not segments:
            logger.warning('outbound_reply_empty', streamId=context.stream.id, turnId=turn)
            return
        receipt = await self._broker.dispatch(OutboundMessage(
            stream=context.stream,
            segments=segments,
        ))
        trace.emit(
            'outbound_delivered',
            platform=receipt.platform,
            streamId=receipt.stream_id,
            turnId=turn,
        )

    async def _maybe_summarize(self, stream_id: int) -> None:
        if stream_id in self._summarizing or not self._summary_provider:
            return
        if self.memory.pending_count(stream_id) < self._summarize_trigger_messages:
            return
        self._summarizing.add(stream_id)
        try:
            batch = self.memory.oldest_pending(
                stream_id,
                self._summarize_batch_messages,
            )
            if len(batch) < 4:
                return
            msgs = [{'role': m['role'], 'content': m['content']} for m in batch]
            episode = await summarize(
                self._summary_provider,
                msgs,
                temperature=self._summary_temperature,
                max_tokens=self._summary_max_tokens,
            )
            if not episode:
                return
            self.memory.add_episode(
                stream_id,
                EpisodeInput(
                    summary=episode.summary,
                    cues=episode.recall_cues,
                    started_at=batch[0]['created_at'],
                    ended_at=batch[-1]['created_at'],
                    message_ids=[message['id'] for message in batch],
                ),
            )
        except Exception:
            pass
        finally:
            self._summarizing.discard(stream_id)


def _extract_lines(raw: str) -> list[dict] | None:
    parser = ResponseParser()
    lines: list[dict] = []
    cur: dict | None = None
    for e in [*parser.push(raw), *parser.flush()]:
        if isinstance(e, SayEvent):
            cur = {'text': '', **({'emotion': e.emotion} if e.emotion else {})}
        elif isinstance(e, TextEvent) and cur is not None:
            cur['text'] += e.value
        elif isinstance(e, SayEndEvent) and cur is not None:
            if cur['text'].strip():
                lines.append({**cur, 'text': cur['text'].strip()})
            cur = None
    return lines if lines else None


def _collect_outbound_segment(
    event: ParseEvent,
    segments: list[str],
    current: list[str] | None,
) -> list[str] | None:
    """按解析器已经识别出的 say 边界收集 QQ 正文，不重新扫描成品文本。"""
    if isinstance(event, SayEvent):
        return []
    if isinstance(event, TextEvent):
        if current is None:
            current = []
        current.append(event.value)
        return current
    if isinstance(event, SayEndEvent):
        if current is not None:
            text = ''.join(current).strip()
            if text:
                segments.append(text)
        return None
    return current


def _plan_to_dict(plan: Any) -> dict | None:
    if plan is None:
        return None
    return {
        'date': plan.date,
        'slots': [{'from': s.from_time, 'doing': s.doing, 'mood': s.mood} for s in plan.slots],
        'bedtimeHint': plan.bedtime_hint,
        'wakeHint': plan.wake_hint,
        'theme': plan.theme,
        'carryOver': plan.carry_over,
    }
