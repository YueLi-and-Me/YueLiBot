"""
对话编排服务。直接移植自 src/main/chat.ts。

持有所有后端资源：按角色拆分的 LLM provider、MemoryStore、Persona、DayPlanService。
通过 WebSocket push 推事件给 Electron 主进程。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

import asyncio
import inspect
import random

from .trace import trace
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
        push_event: Callable[[str, Any], Any],
        speak_audio: Callable[[str, int], Any] | None = None,
        vector: VectorService | None = None,
        cfg: Any | None = None,
    ) -> None:
        self._db = db
        self._chat_provider = chat_provider
        self._proactive_provider = proactive_provider
        self._summary_provider = summary_provider
        self._push_event = push_event
        self._speak_audio = speak_audio
        # 打断时用来叫停已经在播的音频；由 __main__ 注入 TtsService.cancel。
        self._cancel_audio: Callable[[int], Any] | None = None
        # 流式解析时攒当前这句 <say> 的正文，收完整句才送去合成。
        self._speech_buffer: list[str] = []
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
        self.persona = Persona(db)
        self.persona.snapshot_daily()
        self._turn_id = 0
        self._inflight: asyncio.Task | None = None
        self._summarizing = False
        self._activity: Callable[[], str] | None = None
        self._sleep_state: Callable[[], SleepState] | None = None
        self._promise_handler: Callable[[int, str], None] | None = None
        self._schedule: DayPlanService | None = None
        # 会话级人设状态。逐轮重掷会让她的语气一轮一个样，读起来就像每轮
        # 换了个人——这正是「像单次对话」的一部分。
        self._session_started_at: int | None = None
        self._session_tone: str | None = None
        self._session_seed: int = 0
        # 仅供本轮提示词使用；组装后立即清空，避免下一轮重复提起久别。
        self._resumption_gap_ms: int | None = None

    @property
    def ready(self) -> bool:
        return self._chat_provider is not None

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

    def settle_elapsed(self, now: int | None = None, earlier_asleep: bool = False) -> None:
        now = now or current_time()
        before = self.persona.get()
        if self._schedule:
            asleep_hours = self._schedule.sleep_hours_between(before.updated_at, now, earlier_asleep)
        else:
            asleep_hours = 0.0
        self.persona.apply_elapsed(now, asleep_hours)
        self.persona.snapshot_daily(now)

    async def send(self, text: str) -> int:
        trimmed = text.strip()
        if not trimmed:
            return self._turn_id

        self.interrupt()
        turn = self._next_turn()
        mark_turn_start(turn)
        trace.emit('user_input', turnId=turn, text=trimmed)

        if not self._chat_provider:
            await self._emit('chat.error', {'turnId': turn, 'kind': 'error',
                                             'message': '对话未初始化',
                                             'hint': '检查 providers.toml 和 models.toml'})
            return turn

        now = current_time()
        asleep = (self._sleep_state().asleep if self._sleep_state else False)
        self.settle_elapsed(now, asleep)
        self.memory.sweep(now)
        if self._schedule:
            await self._schedule.ensure(now)

        # ★ 必须在 append 之前判定：append 之后 last_message_at() 就是 now，
        #   间隔恒为 0，会话永远不会翻页。
        self._resumption_gap_ms = self._refresh_session(now)

        user_msg_id = self.memory.append_message('user', trimmed, now)
        cancel_event = asyncio.Event()

        async def _run():
            parser = ResponseParser()
            assistant_raw = ''
            side_effects: list[dict] = []
            interrupted = False
            from src.llm_models.openai import LlmError
            try:
                messages = await self._build_messages_with_vector(trimmed, now)
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
                        self._handle_side_effects(event, now, turn, side_effects, trimmed)
                        self._track_speech(event, turn)
                        await self._emit_parse_event(turn, event)
                    if interrupted:
                        break

                if not interrupted:
                    for event in parser.flush():
                        if cancel_event.is_set():
                            interrupted = True
                            break
                        self._handle_side_effects(event, now, turn, side_effects, trimmed)
                        self._track_speech(event, turn)
                        await self._emit_parse_event(turn, event)

                # ★ 中断也要落库。这段话已经显示（甚至念）给用户了，历史里
                #   不能当它没发生过——否则下一轮就是连着两条 user 消息，
                #   模型看不到自己上一句说了什么。副作用（<memory>/<mood>）
                #   在流式过程中已经写库了，话本身更不该丢。
                self._persist_reply(assistant_raw)
                if interrupted:
                    return

                trace.emit('llm_final', turnId=turn, text=assistant_raw)
                render_turn(turn, trimmed, messages, assistant_raw, side_effects)
                try:
                    self.persona.apply_turn(current_time())
                except Exception as exc:
                    # 人格推进失败不该把历史一起拖下水——下面的 except 会删用户消息。
                    logger.warning('persona_apply_turn_failed', turnId=turn, error=str(exc))
                await self._emit('chat.done', {'turnId': turn, 'kind': 'done'})
                asyncio.create_task(self._maybe_summarize())

            except LlmError as exc:
                if exc.kind == 'aborted':
                    self._persist_reply(assistant_raw)
                    return
                self._rollback_or_keep(user_msg_id, assistant_raw)
                hint = _HINTS.get(exc.kind, '')
                trace.emit('llm_error', turnId=turn, errorKind=exc.kind, message=str(exc))
                render_turn_error(turn, trimmed, exc.kind, str(exc))
                await self._emit('chat.error', {'turnId': turn, 'kind': 'error',
                                                 'message': str(exc), 'hint': hint})
            except Exception as exc:
                self._rollback_or_keep(user_msg_id, assistant_raw)
                trace.emit('llm_error', turnId=turn, errorKind='unknown', message=str(exc))
                render_turn_error(turn, trimmed, 'unknown', str(exc))
                await self._emit('chat.error', {'turnId': turn, 'kind': 'error', 'message': str(exc)})

        task = asyncio.create_task(_run())
        task._cancel_event = cancel_event  # type: ignore[attr-defined]
        self._inflight = task
        return turn

    def _refresh_session(self, now: int) -> int | None:
        """跨过静默间隔就开一段新会话，重抽语气和表达样本的随机种子。

        返回本次静默了多久（毫秒）；仍在同一会话内则返回 None。
        首次启动（库里一条消息都没有）也返回 None —— 没有「上一次」可言。
        """
        last = self.memory.last_message_at()
        gap_ms = now - last if last is not None else None
        if (self._session_started_at is None
                or last is None
                or (gap_ms is not None and gap_ms > self._session_gap_ms)):
            self._session_started_at = now
            if self._cfg is None:
                self._session_tone = pick_tone()
            else:
                personality = self._cfg.personality
                self._session_tone = pick_tone(
                    probability=personality.tone_probability,
                    variants=personality.tone_variants,
                )
            self._session_seed = random.randrange(1 << 30)
        if gap_ms is None or gap_ms <= self._session_gap_ms:
            return None
        return gap_ms

    def _take_resumption(self) -> str | None:
        """取走本轮重逢事实，确保它只进入一次系统提示词。"""
        gap_ms = self._resumption_gap_ms
        self._resumption_gap_ms = None
        if gap_ms is None:
            return None
        return describe_resumption(gap_ms)

    def _session_rng(self) -> random.Random:
        """同一会话内给出同一批表达样本；情境识别仍然逐轮进行。"""
        return random.Random(self._session_seed)

    def _persist_reply(self, assistant_raw: str) -> None:
        """把已经吐出去的回复落进历史，补齐流式中断留下的悬空 <say>。"""
        text = close_dangling_say(assistant_raw)
        if text:
            self.memory.append_message('assistant', text, current_time())

    def _rollback_or_keep(self, user_msg_id: int, assistant_raw: str) -> None:
        """出错时决定这一轮留不留。

        一个字都没吐出来 → 整轮回滚，用户那句话也删掉（保持原有意图：
        这轮相当于没发生）。已经有内容显示给用户了 → 两条都留下，
        删掉用户消息反而会让历史里出现「凭空的回复」。
        """
        if close_dangling_say(assistant_raw):
            self._persist_reply(assistant_raw)
        else:
            self.memory.delete_message(user_msg_id)

    def interrupt(self) -> None:
        if self._inflight and not self._inflight.done():
            cancel_ev = getattr(self._inflight, '_cancel_event', None)
            if cancel_ev:
                cancel_ev.set()
        self._inflight = None
        # 半截台词的缓冲不能留到下一轮，否则会把上一句的尾巴念进新回复里。
        self._speech_buffer = []
        if self._cancel_audio:
            try:
                result = self._cancel_audio(self._turn_id)
                if inspect.isawaitable(result):
                    asyncio.create_task(result)
            except Exception as exc:
                logger.warning('cancel_audio_failed', error=str(exc))

    def speak(self, lines: list[dict]) -> int:
        if not lines:
            return self._turn_id
        self.interrupt()
        turn = self._next_turn()
        texts: list[str] = []
        for line in lines:
            asyncio.create_task(self._emit_parse_event(turn, SayEvent(emotion=line.get('emotion'))))
            asyncio.create_task(self._emit_parse_event(turn, TextEvent(value=line['text'])))
            asyncio.create_task(self._emit_parse_event(turn, SayEndEvent()))
            self._dispatch_speech(line['text'], turn)
            texts.append(f'<say>{line["text"]}</say>')
        asyncio.create_task(self._emit('chat.done', {'turnId': turn, 'kind': 'done'}))
        self.memory.append_message('assistant', ''.join(texts))
        return turn

    async def compose_proactive(self, situation: str) -> list[dict] | None:
        if not self._proactive_provider:
            return None
        now = current_time()
        self._resumption_gap_ms = self._refresh_session(now)
        if self._schedule:
            await self._schedule.ensure(now)
        persona_desc = describe_persona(self.persona.get())
        acquaintance = describe_acquaintance(self.memory.first_seen_at, now)
        schedule_desc = (self._schedule.describe(now, self.current_sleep())
                         if self._schedule else '')
        base_prompt = build_system_prompt(
            now=datetime.fromtimestamp(now / 1000),
            persona=persona_desc,
            acquaintance=acquaintance,
            facts=[fact.content for fact in self.memory.top_facts(5, now)],
            episodes=[episode.summary for episode in self.memory.recent_episodes(2)],
            schedule=schedule_desc,
            expression_habits=render_expression_habits(
                select_expression_habits(situation, proactive=True, limit=3, rng=self._session_rng())
            ),
            tone=self._session_tone,
            resumption=self._take_resumption(),
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
        memories = [{'content': f.content, 'frozen': f.frozen} for f in self.memory.all_facts(now)]
        today = self._schedule.get(now) if self._schedule else None
        return {
            'entries': self.memory.all_episodes(),
            'today': _plan_to_dict(today) if today else None,
            'memories': memories,
            'now': now,
        }

    def observability_snapshot(self, now: int | None = None) -> dict:
        now = now or current_time()
        s = self.persona.get()
        fc = self.memory.fact_count()
        return {
            'now': now,
            'persona': {'state': s.__dict__, 'description': describe_persona(s)},
            'schedule': _plan_to_dict(self._schedule.get(now)) if self._schedule else None,
            'memory': {
                'semantic': [f.__dict__ for f in self.memory.all_facts(now)],
                'episodes': len(self.memory.all_episodes()),
                'workingMessages': self.memory.pending_count(),
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

    async def _build_messages_with_vector(self, query: str, now: int) -> list[dict]:
        """向量召回版本的消息构建。_build_messages 的异步替代。"""
        query_embedding = await self._vector.embed_query(query)
        facts = self.memory.recall_facts(
            query, self._fact_recall_limit, now, query_embedding=query_embedding
        )
        recalled = self.memory.recall_episodes(query, self._recalled_episode_limit)
        recent = self.memory.recent_episodes(self._recent_episode_limit)
        seen_ids: set[int] = set()
        episodes = []
        for e in [*recalled, *recent]:
            if e.id not in seen_ids:
                seen_ids.add(e.id)
                episodes.append(e)
        episodes = episodes[:self._episode_context_limit]
        persona_desc = describe_persona(self.persona.get())
        acquaintance = describe_acquaintance(self.memory.first_seen_at, now)
        schedule_desc = (self._schedule.describe(now, self.current_sleep()) if self._schedule else None)
        resumption = self._take_resumption()
        system = build_system_prompt(
            now=datetime.fromtimestamp(now / 1000),
            persona=persona_desc,
            acquaintance=acquaintance,
            facts=[f.content for f in facts],
            episodes=[e.summary for e in episodes],
            activity=(self._activity() if self._activity else None),
            schedule=schedule_desc,
            expression_habits=render_expression_habits(
                select_expression_habits(query, rng=self._session_rng())
            ),
            tone=self._session_tone,
            resumption=resumption,
            **self._prompt_config_kwargs(),
        )
        # ★ 读时修复：不假设历史是干净的。库里已经存在的坏历史（每一次打断
        #   都损坏过一轮）只能在这里救回来，写入端的修复管不到已经写坏的部分。
        #   幂等，对干净历史没有副作用。
        wm = self.memory.working_memory(self._working_memory_messages)
        history = normalize_history({'role': m.role, 'content': m.content} for m in wm)
        return [{'role': 'system', 'content': system}, *fit_char_budget(history)]

    def _handle_side_effects(
        self, event: ParseEvent, now: int, turn: int, sink: list[dict] | None = None,
        source_text: str | None = None,
    ) -> None:
        if isinstance(event, MemoryEvent) and event.content:
            memory_kind = event.memory_type or '未分类'
            self.memory.add_fact(FactInput(content=event.content, kind=memory_kind), now)
            trace.emit('memory_fact', turnId=turn, content=event.content, memoryKind=memory_kind)
            if sink is not None:
                sink.append({'kind': 'memory_fact', 'content': event.content, 'memoryKind': memory_kind})
        elif isinstance(event, MoodEvent):
            self.persona.apply_mood(MoodDelta(favor=event.favor, energy=event.energy), now)
            trace.emit('mood_delta', turnId=turn, favor=event.favor, energy=event.energy)
            if sink is not None:
                sink.append({'kind': 'mood_delta', 'favor': event.favor, 'energy': event.energy})
        elif isinstance(event, PromiseEvent):
            if self._promise_handler is None:
                logger.warning('promise_handler_missing', turnId=turn)
                return
            if source_text is None:
                raise ValueError('约定事件必须关联本轮用户原话')
            self._promise_handler(event.at, source_text)
            trace.emit('promise_stashed', turnId=turn, at=event.at, subject=source_text)
            if sink is not None:
                sink.append({'kind': 'promise_stashed', 'at': event.at, 'subject': source_text})

    def _dispatch_speech(self, text: str, turn: int) -> None:
        """把一句台词送去合成。

        ★ 这里必须容忍同步和协程两种回调：注入进来的 TtsService.speak 是同步的
          （它自己内部起 task），而此前调用点写的是
          `asyncio.create_task(self._speak_audio(...))` —— 对同步函数来说等于
          `create_task(None)`，直接抛 TypeError。主动搭话那条语音链路一直是
          这么坏掉的。
        """
        if not self._speak_audio:
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

    def _track_speech(self, event: ParseEvent, turn: int) -> None:
        """在流式解析过程中攒出完整台词，每收完一个 <say> 就送去合成。

        ★ 此前这里是个空 stub，导致正常对话**完全不发声**——_speak_audio
          全仓库只在 speak()（主动搭话）里被调用过。按 <say> 分句送，而不是
          等整段回复收完，是为了让她开口的延迟和字幕对得上。
        """
        if not self._speak_audio:
            return
        if isinstance(event, SayEvent):
            self._speech_buffer = []
        elif isinstance(event, TextEvent):
            self._speech_buffer.append(event.value)
        elif isinstance(event, SayEndEvent):
            line = ''.join(self._speech_buffer)
            self._speech_buffer = []
            self._dispatch_speech(line, turn)

    async def _emit(self, channel: str, payload: Any) -> None:
        try:
            await self._push_event(channel, payload)
        except Exception as exc:
            logger.warning('emit_failed', channel=channel, error=str(exc))

    async def _emit_parse_event(self, turn: int, event: ParseEvent) -> None:
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
        await self._emit('chat.event', ev)

    async def _maybe_summarize(self) -> None:
        if self._summarizing or not self._summary_provider:
            return
        if self.memory.pending_count() < self._summarize_trigger_messages:
            return
        self._summarizing = True
        try:
            batch = self.memory.oldest_pending(self._summarize_batch_messages)
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
            self.memory.add_episode(EpisodeInput(
                summary=episode.summary, cues=episode.recall_cues,
                started_at=batch[0]['created_at'], ended_at=batch[-1]['created_at'],
                message_ids=[m['id'] for m in batch],
            ))
        except Exception:
            pass
        finally:
            self._summarizing = False


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
